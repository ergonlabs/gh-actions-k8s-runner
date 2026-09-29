#!/usr/bin/env python3
"""Per-container resource sampler for ARC runner pods (see manifests/47-resource-sampler.yaml).

Every INTERVAL seconds, reads the cgroup v2 counters of every container in every live runner
pod straight from the host's cgroup tree, plus node-wide context from /proc, and appends one
JSON line to /data/<pod-name>/samples.jsonl. The runner container mounts its own
/data/<pod-name> read-only at /resource-metrics, and runner-image/resource-report/render.py
turns it into the per-job HTML report.

Counters are stored RAW (cumulative usec/bytes) and differentiated at render time, so a
missed tick never corrupts a rate — it just widens one interval.

stdlib only; runs on python:3-alpine.
"""
import gzip
import json
import os
import shutil
import ssl
import sys
import time
import urllib.parse
import urllib.request

INTERVAL = float(os.environ.get("SAMPLE_INTERVAL_S", "1"))
API_REFRESH_S = 5
NAMESPACE = os.environ.get("RUNNER_NAMESPACE", "arc-runners")
SELECTOR = "actions-ephemeral-runner=True"
POD_PREFIX = "ergonlabs-k8s-"
CGROUP_ROOT = os.environ.get("CGROUP_ROOT", "/host-cgroup")  # host's /sys/fs/cgroup/kubepods.slice
DATA = os.environ.get("DATA_DIR", "/data")
ARCHIVE = os.path.join(DATA, "_archive")
ARCHIVE_DAYS = int(os.environ.get("ARCHIVE_DAYS", "30"))
# A pod's dir is only archived once it has stopped being written for this long AND an
# individual GET confirms the pod is gone (same re-check-before-delete rule as store-gc).
ARCHIVE_GRACE_S = 180

SA = "/var/run/secrets/kubernetes.io/serviceaccount"


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


# ---------------------------------------------------------------- kubernetes API

_ctx = None


def api(path):
    global _ctx
    if _ctx is None:
        _ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with open(f"{SA}/token") as f:
        token = f.read().strip()
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    req = urllib.request.Request(
        f"https://{host}:{port}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, context=_ctx, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def parse_cpu(q):
    if q is None:
        return None
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


_MEM = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def parse_mem(q):
    if q is None:
        return None
    q = str(q)
    for suf in ("Ki", "Mi", "Gi", "Ti", "k", "M", "G", "T"):
        if q.endswith(suf):
            return int(float(q[: -len(suf)]) * _MEM[suf])
    return int(float(q))


def list_runner_pods():
    """-> {pod_name: pod_info}. Raises on API failure (caller must not treat that as 'no pods')."""
    sel = urllib.parse.quote(SELECTOR)
    items = api(f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector={sel}")["items"]
    pods = {}
    for p in items:
        md, spec, st = p["metadata"], p["spec"], p.get("status", {})
        if not md["name"].startswith(POD_PREFIX):
            continue
        specs = {c["name"]: c for c in spec.get("containers", []) + spec.get("initContainers", [])}
        containers = {}
        for cs in st.get("containerStatuses", []) + st.get("initContainerStatuses", []):
            cid = (cs.get("containerID") or "").split("://")[-1]
            if not cid or "running" not in cs.get("state", {}):
                continue  # finished init containers (init-dind-externals) have no live cgroup
            res = specs.get(cs["name"], {}).get("resources", {})
            containers[cs["name"]] = {
                "id": cid,
                "restarts": cs.get("restartCount", 0),
                "cpu_request": parse_cpu(res.get("requests", {}).get("cpu")),
                "mem_request": parse_mem(res.get("requests", {}).get("memory")),
            }
        pods[md["name"]] = {
            "pod": md["name"],
            "uid": md["uid"],
            "pool": md.get("labels", {}).get("actions.github.com/scale-set-name"),
            "node": spec.get("nodeName"),
            "qos": st.get("qosClass", "Burstable"),
            "created": md.get("creationTimestamp"),
            "containers": containers,
        }
    return pods


# ---------------------------------------------------------------- cgroup reads


def pod_cgroup_dir(uid, qos):
    u = uid.replace("-", "_")
    q = qos.lower()
    if q == "guaranteed":
        return os.path.join(CGROUP_ROOT, f"kubepods-pod{u}.slice")
    return os.path.join(CGROUP_ROOT, f"kubepods-{q}.slice", f"kubepods-{q}-pod{u}.slice")


def read(path):
    with open(path) as f:
        return f.read()


def kv(path):
    out = {}
    for line in read(path).splitlines():
        k, _, v = line.partition(" ")
        out[k] = v
    return out


def psi_total(path):
    """-> (some_total_usec, full_total_usec). 'full' is absent for cpu on 5.15."""
    some = full = 0
    for line in read(path).splitlines():
        kind, *fields = line.split()
        tot = int(dict(f.split("=") for f in fields)["total"])
        if kind == "some":
            some = tot
        else:
            full = tot
    return some, full


def limits(d):
    cpu = read(os.path.join(d, "cpu.max")).split()
    mem = read(os.path.join(d, "memory.max")).strip()
    return (
        None if cpu[0] == "max" else int(cpu[0]) / int(cpu[1]),
        None if mem == "max" else int(mem),
    )


def sample_cgroup(d):
    """Compact raw counters for one cgroup dir (short keys: this is written once per second
    per container, for every runner pod)."""
    cs = kv(os.path.join(d, "cpu.stat"))
    ms = kv(os.path.join(d, "memory.stat"))
    ev = kv(os.path.join(d, "memory.events"))
    mps, mpf = psi_total(os.path.join(d, "memory.pressure"))
    cps, _ = psi_total(os.path.join(d, "cpu.pressure"))
    return {
        "u": int(cs["usage_usec"]),
        "p": int(cs.get("nr_periods", 0)),
        "th": int(cs.get("nr_throttled", 0)),
        "tu": int(cs.get("throttled_usec", 0)),
        "m": int(read(os.path.join(d, "memory.current"))),
        "a": int(ms.get("anon", 0)),
        "f": int(ms.get("file", 0)),
        "if": int(ms.get("inactive_file", 0)),
        "sh": int(ms.get("shmem", 0)),
        "sl": int(ms.get("slab", 0)),
        "o": int(ev.get("oom_kill", 0)),
        "mx": int(ev.get("max", 0)),
        "mps": mps,
        "mpf": mpf,
        "cps": cps,
    }


def sample_node():
    mi = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        mi[k] = int(v.split()[0]) * 1024
    cpu = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    idle = cpu[3] + cpu[4]  # idle + iowait
    mps, mpf = psi_total("/proc/pressure/memory")
    cps, _ = psi_total("/proc/pressure/cpu")
    return {
        "mt": mi["MemTotal"],
        "ma": mi["MemAvailable"],
        "ct": sum(cpu[:8]),  # jiffies; guest time is already inside user/nice
        "ci": idle,
        "mps": mps,
        "mpf": mpf,
        "cps": cps,
    }


# ---------------------------------------------------------------- per-pod output


def ensure_dir(pod):
    d = os.path.join(DATA, pod)
    os.makedirs(d, exist_ok=True)
    # kubelet may have created it first (root, restrictive mode) for the runner's subPath
    # mount; the runner reads as uid 1001, so it must be world-readable.
    os.chmod(d, 0o755)
    return d


def write_meta(pod, info, cg):
    d = ensure_dir(pod)
    meta = {k: v for k, v in info.items() if k != "containers"}
    meta["interval_s"] = INTERVAL
    meta["containers"] = {}
    for name, c in info["containers"].items():
        cd = os.path.join(cg, f"cri-containerd-{c['id']}.scope")
        try:
            cpu_lim, mem_lim = limits(cd)
        except OSError:
            continue
        meta["containers"][name] = dict(c, cpu_limit=cpu_lim, mem_limit=mem_lim)
    try:
        meta["pod_cpu_limit"], meta["pod_mem_limit"] = limits(cg)
    except OSError:
        pass
    tmp = os.path.join(d, ".meta.json.tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(d, "meta.json"))


class PodWriter:
    def __init__(self, pod):
        self.path = os.path.join(ensure_dir(pod), "samples.jsonl")
        self.f = open(self.path, "a", buffering=1)
        os.chmod(self.path, 0o644)

    def write(self, rec):
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- archive / retention


def archive_dead(live):
    """Compress finished pods' data into _archive/<day>/ and drop the live dir.

    Deletion is only of this sampler's own metrics, but a wrongly-archived LIVE pod would lose
    its job report, so the same guard as store-gc applies: re-check each pod individually right
    before touching it, and never act on a failed pod listing (`live` is None then).
    """
    if live is None:
        return
    now = time.time()
    for name in os.listdir(DATA):
        d = os.path.join(DATA, name)
        if not name.startswith(POD_PREFIX) or name in live or not os.path.isdir(d):
            continue
        s = os.path.join(d, "samples.jsonl")
        last = os.path.getmtime(s) if os.path.exists(s) else os.path.getmtime(d)
        if now - last < ARCHIVE_GRACE_S:
            continue
        try:
            if api(f"/api/v1/namespaces/{NAMESPACE}/pods/{name}") is not None:
                continue
        except Exception as e:  # can't tell -> don't touch
            log(f"archive: liveness check failed for {name}: {e}")
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(last))
        dest = os.path.join(ARCHIVE, day)
        os.makedirs(dest, exist_ok=True)
        if os.path.exists(s):
            with open(s, "rb") as src, gzip.open(os.path.join(dest, f"{name}.samples.jsonl.gz"), "wb") as out:
                shutil.copyfileobj(src, out)
        m = os.path.join(d, "meta.json")
        if os.path.exists(m):
            shutil.copy(m, os.path.join(dest, f"{name}.meta.json"))
        shutil.rmtree(d, ignore_errors=True)
        log(f"archived {name} -> {dest}")

    cutoff = time.strftime("%Y-%m-%d", time.gmtime(now - ARCHIVE_DAYS * 86400))
    if os.path.isdir(ARCHIVE):
        for day in os.listdir(ARCHIVE):
            if len(day) == 10 and day < cutoff:
                shutil.rmtree(os.path.join(ARCHIVE, day), ignore_errors=True)
                log(f"expired archive {day}")


# ---------------------------------------------------------------- main loop


def main():
    log(f"sampler starting: interval={INTERVAL}s cgroup_root={CGROUP_ROOT} data={DATA}")
    pods, writers, meta_sig = {}, {}, {}
    next_api = 0.0
    next_archive = 0.0
    tick = time.monotonic()
    while True:
        now_wall = time.time()
        if time.monotonic() >= next_api:
            next_api = time.monotonic() + API_REFRESH_S
            try:
                pods = list_runner_pods()
                live = set(pods)
            except Exception as e:
                log(f"pod list failed (keeping previous set): {e}")
                live = None
            for name in list(writers):
                if name not in pods:
                    writers.pop(name).close()
                    meta_sig.pop(name, None)
            for name, info in pods.items():
                cg = pod_cgroup_dir(info["uid"], info["qos"])
                sig = json.dumps(info["containers"], sort_keys=True)
                if meta_sig.get(name) != sig and os.path.isdir(cg):
                    try:
                        write_meta(name, info, cg)
                        meta_sig[name] = sig
                    except OSError as e:
                        log(f"meta {name}: {e}")
            if time.monotonic() >= next_archive:
                next_archive = time.monotonic() + 60
                try:
                    archive_dead(live)
                except Exception as e:
                    log(f"archive pass failed: {e}")

        try:
            node = sample_node()
        except OSError:
            node = None
        for name, info in pods.items():
            cg = pod_cgroup_dir(info["uid"], info["qos"])
            if not os.path.isdir(cg) or name not in meta_sig:
                continue
            rec = {"t": round(now_wall, 3), "c": {}}
            try:
                rec["pod"] = sample_cgroup(cg)
            except OSError:
                continue  # pod cgroup vanished between listing and reading
            for cname, c in info["containers"].items():
                try:
                    rec["c"][cname] = sample_cgroup(os.path.join(cg, f"cri-containerd-{c['id']}.scope"))
                except OSError:
                    pass
            if node:
                rec["node"] = node
            try:
                if name not in writers:
                    writers[name] = PodWriter(name)
                writers[name].write(rec)
            except OSError as e:
                log(f"write {name}: {e}")
                writers.pop(name, None)

        tick += INTERVAL
        delay = tick - time.monotonic()
        if delay < 0:  # fell behind (slow API call); resync rather than burst
            tick = time.monotonic()
            delay = 0
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
