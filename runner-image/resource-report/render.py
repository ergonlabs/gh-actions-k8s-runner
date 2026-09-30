#!/usr/bin/env python3
"""Render a per-job CPU/memory report for an ARC runner pod.

Inputs:
  --metrics-dir  this pod's slice of manifests/47-resource-sampler.yaml's output
                 (meta.json + samples.jsonl; mounted read-only at /resource-metrics)
  --diag-dir     the runner's _diag dir; the newest Worker_*.log gives the job's display
                 name, start time and top-level step boundaries

Outputs (in --out):
  resource-report.html   self-contained single page, inline SVG charts, no network needed
  resource-samples.csv   the derived per-second series behind the charts
and optionally a markdown digest appended to --summary (the job summary).

Exit status: 0 rendered, 3 no metrics for this pod (e.g. a hosted runner), 1 other error.
Callers (the job-completed hook, the resource-report action) must never let a non-zero exit
fail a job — this is observability, not a gate.

stdlib only: it runs inside the runner image and must also degrade cleanly anywhere else.
"""
import argparse
import bisect
import calendar
import csv
import glob
import html
import json
import os
import re
import sys
import time

KNOWN_ORDER = ["runner", "dind", "buildkitd", "doppler-log-capture"]
MAX_POINTS = 1500  # per chart series; memory is bucketed by MAX so peaks survive downsampling

GIB = 2**30
MIB = 2**20


# ------------------------------------------------------------------ inputs


def load_samples(path, t0=None, t1=None):
    out = []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue  # a torn final line while the sampler is mid-write
            if t0 is not None and r["t"] < t0:
                continue
            if t1 is not None and r["t"] > t1:
                break
            out.append(r)
    return out


def parse_worker_log(diag_dir):
    """-> (job_display_name, job_start_epoch, [(step_name, start_epoch)])"""
    logs = sorted(glob.glob(os.path.join(diag_dir, "Worker_*.log")))
    if not logs:
        return None, None, []
    ts_re = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)Z ")
    step_re = re.compile(r"StepsRunner\] Processing step: DisplayName='(.*)'\s*$")
    name_re = re.compile(r'"jobDisplayName":\s*"(.*)"')

    def ts(line):
        m = ts_re.match(line)
        return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) if m else None

    name, start, steps = None, None, []
    with open(logs[-1], errors="replace") as f:
        for line in f:
            if start is None:
                start = ts(line)
            if name is None:
                m = name_re.search(line)
                if m:
                    name = json.loads(f'"{m.group(1)}"')
            m = step_re.search(line)
            if m:
                t = ts(line)
                if t is not None:
                    steps.append((m.group(1), t))
    return name, start, steps


# ------------------------------------------------------------------ derivation


def cg_get(r, k):
    """Counters for key k in one sample: "pod", a k8s container name, "x:<name>" (a container
    dind started, outside the pod's cgroup) or "k:<name>" (a pod inside the kind cluster)."""
    if k == "pod":
        return r.get("pod")
    if k.startswith("x:"):
        return r.get("x", {}).get(k[2:])
    if k.startswith("k:"):
        return r.get("k", {}).get(k[2:])
    return r.get("c", {}).get(k)


def derive(samples, containers):
    """Turn raw cumulative counters into per-interval series. Each series list is aligned
    with `t` (one entry per sample after the first); None where a value isn't available
    (container not yet started, or a counter reset from a container restart)."""
    keys = containers + ["pod"]
    t = []
    s = {k: {f: [] for f in ("cpu", "thr", "ws", "anon", "file", "inact", "mpsi", "cpsi")} for k in keys}
    node = {f: [] for f in ("mem_used_pct", "cpu_busy_pct", "mpsi", "cpsi")}

    get = cg_get

    for prev, cur in zip(samples, samples[1:]):
        dt = cur["t"] - prev["t"]
        if dt <= 0:
            continue
        t.append(cur["t"])
        for k in keys:
            a, b = get(prev, k), get(cur, k)
            d = s[k]
            if b is None:
                for f in d:
                    d[f].append(None)
                continue
            d["ws"].append(max(0, b["m"] - b["if"]))
            d["anon"].append(b["a"])
            d["file"].append(b["f"])
            d["inact"].append(b["if"])
            if a is None or b["u"] < a["u"]:  # first sample of a (re)started container
                for f in ("cpu", "thr", "mpsi", "cpsi"):
                    d[f].append(None)
                continue
            d["cpu"].append((b["u"] - a["u"]) / dt / 1e6)
            if "p" not in b:  # nested/kind samples carry no throttling or PSI counters
                for f in ("thr", "mpsi", "cpsi"):
                    d[f].append(None)
                continue
            dp = b["p"] - a["p"]
            d["thr"].append(100.0 * (b["th"] - a["th"]) / dp if dp > 0 else 0.0)
            d["mpsi"].append(min(100.0, (b["mps"] - a["mps"]) / dt / 1e4))
            d["cpsi"].append(min(100.0, (b["cps"] - a["cps"]) / dt / 1e4))
        a, b = prev.get("node"), cur.get("node")
        if a and b:
            dct = b["ct"] - a["ct"]
            node["mem_used_pct"].append(100.0 * (b["mt"] - b["ma"]) / b["mt"])
            node["cpu_busy_pct"].append(100.0 * (dct - (b["ci"] - a["ci"])) / dct if dct > 0 else None)
            node["mpsi"].append(min(100.0, (b["mps"] - a["mps"]) / dt / 1e4))
            node["cpsi"].append(min(100.0, (b["cps"] - a["cps"]) / dt / 1e4))
        else:
            for f in node:
                node[f].append(None)
    return t, s, node


def vals(xs):
    return [x for x in xs if x is not None]


def pct(xs, q):
    xs = sorted(vals(xs))
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(q / 100.0 * (len(xs) - 1))))]


def peak(xs):
    xs = vals(xs)
    return max(xs) if xs else None


def rolling_mean(xs, n):
    """Trailing mean over n samples, ignoring gaps — 1 s CPU readings are too spiky to call
    a 'peak' on their own."""
    out, window = [], []
    for x in xs:
        window.append(x)
        if len(window) > n:
            window.pop(0)
        v = vals(window)
        out.append(sum(v) / len(v) if v else None)
    return out


def counter_delta(samples, k, field):
    first = last = None
    for r in samples:
        c = cg_get(r, k)
        if c is None or field not in c:
            continue
        if first is None:
            first = c[field]
        last = c[field]
    return (last - first) if first is not None else 0


def throttle_total(samples, k):
    """Share of CFS periods throttled over the whole window (not an average of 1 s ratios)."""
    p = counter_delta(samples, k, "p")
    return 100.0 * counter_delta(samples, k, "th") / p if p > 0 else None


def downsample(t, series_list, aggs):
    n = len(t)
    if n <= MAX_POINTS:
        return t, series_list
    size = -(-n // MAX_POINTS)
    nt, ns = [], [[] for _ in series_list]
    for i in range(0, n, size):
        nt.append(t[min(n - 1, i + size - 1)])
        for j, (xs, agg) in enumerate(zip(series_list, aggs)):
            chunk = vals(xs[i : i + size])
            if not chunk:
                ns[j].append(None)
            elif agg == "max":
                ns[j].append(max(chunk))
            else:
                ns[j].append(sum(chunk) / len(chunk))
    return nt, ns


# ------------------------------------------------------------------ formatting


def fmt_bytes(b):
    if b is None:
        return "–"
    if b >= GIB:
        return f"{b / GIB:.2f} GiB"
    return f"{b / MIB:.0f} MiB"


def fmt_cores(c):
    return "–" if c is None else f"{c:.2f}"


def fmt_pct(p):
    return "–" if p is None else f"{p:.0f}%" if p >= 10 else f"{p:.1f}%"


def fmt_dur(s):
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s" if m else f"{sec}s"


def of_limit(v, lim):
    return None if v is None or not lim else 100.0 * v / lim


# ------------------------------------------------------------------ report model

# Kubernetes' random name suffixes use this vowel-free alphabet, so stripping only suffixes
# drawn from it can't eat a real name part like "-plane".
_K8S_RAND = "[bcdfghjklmnpqrstvwxz2456789]"
_WORKLOAD_RES = [
    re.compile(rf"^(.+)-{_K8S_RAND}{{6,10}}-{_K8S_RAND}{{5}}$"),  # Deployment pod
    re.compile(rf"^(.+)-{_K8S_RAND}{{5}}$"),                      # DaemonSet / Job pod
    re.compile(r"^(.+)-\d+$"),                                    # StatefulSet pod
]


def workload_of(pod_name):
    for rx in _WORKLOAD_RES:
        m = rx.match(pod_name)
        if m:
            return m.group(1)
    return pod_name


def sum_series(parts):
    """Element-wise sum ignoring gaps; None only where every part is None."""
    out = []
    for xs in zip(*parts):
        v = [x for x in xs if x is not None]
        out.append(sum(v) if v else None)
    return out



def build(metrics_dir, diag_dir):
    meta_p = os.path.join(metrics_dir, "meta.json")
    samp_p = os.path.join(metrics_dir, "samples.jsonl")
    if not (os.path.exists(meta_p) and os.path.exists(samp_p)):
        return None
    with open(meta_p) as f:
        meta = json.load(f)
    job_name, job_start, steps = parse_worker_log(diag_dir) if diag_dir else (None, None, [])
    # A second of slack before the first log line: the runner starts logging a beat after
    # the job is assigned, and the first sample interval then covers the true start.
    samples = load_samples(samp_p, t0=(job_start - 2) if job_start else None)
    if len(samples) < 3:
        return None
    t_start = job_start or samples[0]["t"]
    t_end = samples[-1]["t"]

    present, xpresent, kpresent = set(), set(), set()
    for r in samples:
        present.update(r.get("c", {}).keys())
        xpresent.update(r.get("x", {}).keys())
        kpresent.update(r.get("k", {}).keys())
    containers = [c for c in KNOWN_ORDER if c in present] + sorted(present - set(KNOWN_ORDER))
    xkeys = [f"x:{n}" for n in sorted(xpresent)]
    kkeys = [f"k:{n}" for n in sorted(kpresent)]
    t, s, node = derive(samples, containers + xkeys + kkeys)
    cmeta = meta.get("containers", {})
    disp = {k: (f"dind › {k[2:]}" if k.startswith("x:") else k) for k in containers + xkeys}
    disp["pod"] = "pod total (k8s)" if xkeys else "pod total"
    disp["job"] = "job total"
    chart_keys = containers + xkeys
    names = [disp[k] for k in chart_keys]

    # "job" = the pod's own cgroups + everything dind started outside them. Only differs from
    # "pod" on large-pool jobs that run containers (e.g. the E2E kind cluster).
    s["job"] = {f: sum_series([s[k][f] for k in ["pod"] + xkeys]) for f in ("ws", "anon", "inact", "cpu")}
    tot_key = "job" if xkeys else "pod"

    # ---- per-container stats
    rows = []
    for c in containers + xkeys + ["pod"] + (["job"] if xkeys else []):
        m = {} if c.startswith("x:") or c == "job" else cmeta.get(c, {}) if c != "pod" else {
            "mem_limit": meta.get("pod_mem_limit"),
            "cpu_limit": meta.get("pod_cpu_limit"),
            "mem_request": sum((cmeta[x].get("mem_request") or 0) for x in containers if x in cmeta) or None,
            "cpu_request": sum((cmeta[x].get("cpu_request") or 0) for x in containers if x in cmeta) or None,
        }
        d = s[c]
        pk = peak(d["ws"])
        pk_i = d["ws"].index(pk) if pk is not None else None
        parts = ["pod"] + xkeys if c == "job" else [c]
        cpu_used = sum(counter_delta(samples, x, "u") for x in parts) / 1e6
        rows.append({
            "name": disp[c],
            "nested": c.startswith("x:"),
            "total": c in ("pod", "job"),
            "mem_request": m.get("mem_request"),
            "mem_limit": m.get("mem_limit"),
            "ws_peak": pk,
            "ws_peak_at": (t[pk_i] - t_start) if pk_i is not None else None,
            "ws_p95": pct(d["ws"], 95),
            "ws_p50": pct(d["ws"], 50),
            "anon_peak": peak(d["anon"]),
            "ws_peak_pct": of_limit(pk, m.get("mem_limit")),
            "oom_kills": sum(counter_delta(samples, x, "o") for x in parts),
            "mem_max_events": sum(counter_delta(samples, x, "mx") for x in parts),
            "mem_psi_peak": peak(rolling_mean(d.get("mpsi", []), 10)),
            "cpu_request": m.get("cpu_request"),
            "cpu_limit": m.get("cpu_limit"),
            "cpu_avg": cpu_used / max(1e-9, t_end - samples[0]["t"]),
            "cpu_p95": pct(rolling_mean(d["cpu"], 5), 95),
            "cpu_peak": peak(rolling_mean(d["cpu"], 5)),
            "cpu_seconds": cpu_used,
            "throttled_pct": throttle_total(samples, c) if m.get("cpu_limit") else None,
            "cpu_psi_peak": peak(rolling_mean(d.get("cpsi", []), 10)),
        })

    # ---- kind-cluster workloads (pods grouped by owning Deployment/StatefulSet/DaemonSet)
    groups = {}
    for k in kkeys:
        groups.setdefault(workload_of(k[2:]), []).append(k)
    window = max(1e-9, t_end - samples[0]["t"])
    kind_rows, wl_series = [], {}
    for wl, ks in groups.items():
        ws = sum_series([s[k]["ws"] for k in ks])
        cpu = sum_series([s[k]["cpu"] for k in ks])
        wl_series[wl] = (ws, cpu)
        used = sum(counter_delta(samples, k, "u") for k in ks) / 1e6
        kind_rows.append({
            "name": wl, "pods": len(ks),
            "ws_peak": peak(ws), "ws_p95": pct(ws, 95), "ws_p50": pct(ws, 50),
            "anon_peak": peak(sum_series([s[k]["anon"] for k in ks])),
            "oom_kills": sum(counter_delta(samples, k, "o") for k in ks),
            "cpu_avg": used / window, "cpu_peak": peak(rolling_mean(cpu, 5)), "cpu_seconds": used,
        })
    kind_rows.sort(key=lambda r: -(r["ws_peak"] or 0))

    # ---- per-step stats
    step_rows = []
    bounds = [(n, st) for n, st in steps if st <= t_end]
    for i, (n, st) in enumerate(bounds):
        en = bounds[i + 1][1] if i + 1 < len(bounds) else t_end
        a, b = bisect.bisect_left(t, st), bisect.bisect_right(t, en)
        if b <= a:
            step_rows.append({"i": i + 1, "name": n, "start": st - t_start, "dur": en - st})
            continue
        sl = lambda xs: xs[a:b]  # noqa: E731
        top = None
        for c in chart_keys:
            p = peak(sl(s[c]["ws"]))
            if p is not None and (top is None or p > top[1]):
                top = (disp[c], p)
        tot = s[tot_key]
        cpu5 = rolling_mean(tot["cpu"], 5)[a:b]
        step_rows.append({
            "i": i + 1, "name": n, "start": st - t_start, "dur": en - st,
            "ws_peak": peak(sl(tot["ws"])),
            "top": top[0] if top and len(chart_keys) > 1 else None,
            "cpu_avg": (sum(vals(sl(tot["cpu"]))) / max(1, len(vals(sl(tot["cpu"]))))) if vals(sl(tot["cpu"])) else None,
            "cpu_peak": peak(cpu5),
            "thr": peak(rolling_mean(sl(s["pod"]["thr"]), 5)) if meta.get("pod_cpu_limit") else None,
            "mpsi": peak(rolling_mean(sl(s["pod"]["mpsi"]), 5)),
        })

    # ---- chart series (downsampled; memory bucketed by max so peaks survive)
    rel = [max(0.0, x - t_start) for x in t]  # the first interval can straddle job start
    series, aggs, idx = [], [], {}

    def add(key, xs, agg):
        idx[key] = len(series)
        series.append(xs)
        aggs.append(agg)

    for c in chart_keys:
        add(f"ws:{c}", s[c]["ws"], "max")
        add(f"cpu:{c}", s[c]["cpu"], "mean")
        add(f"thr:{c}", rolling_mean(s[c]["thr"], 5), "mean")
    add("job:ws", s["job"]["ws"], "max")
    add("job:cpu", s["job"]["cpu"], "mean")
    p, jt = s["pod"], s[tot_key]
    add("pod:anon", jt["anon"], "max")
    add("pod:other", [None if w is None or a is None else max(0, w - a) for w, a in zip(jt["ws"], jt["anon"])], "max")
    add("pod:inact", jt["inact"], "max")
    top_wl = [r["name"] for r in kind_rows[:7]]
    rest = [wl for wl in wl_series if wl not in top_wl]
    for wl in top_wl:
        add(f"wl:{wl}", wl_series[wl][0], "max")
    if rest:
        add("wl:__other", sum_series([wl_series[wl][0] for wl in rest]), "max")
    add("pod:mpsi", p["mpsi"], "mean")
    add("pod:cpsi", p["cpsi"], "mean")
    for f in node:
        add(f"node:{f}", node[f], "mean")
    dt, ds = downsample(rel, series, aggs)

    def S(key):
        return [None if v is None else round(v, 4) for v in ds[idx[key]]]

    lim = {c: cmeta.get(c, {}) for c in chart_keys}
    job_line = [{"name": "job total", "ink": True, "values": S("job:ws")}] if xkeys else []
    job_cpu_line = [{"name": "job total", "ink": True, "values": S("job:cpu")}] if xkeys else []
    charts = [
        {
            "id": "mem", "title": "Memory working set by container", "unit": "bytes",
            "sub": "memory.current − inactive_file: what kubelet evicts on and the OOM killer counts. Bucketed by max, so spikes aren't averaged away."
                   + (" “dind ›” series are containers dind started (e.g. the kind node): outside the pod's cgroups, so no k8s limit applies to them." if xkeys else ""),
            "series": [{"name": disp[c], "slot": slot_for(disp[c], names), "values": S(f"ws:{c}")} for c in chart_keys] + job_line,
            "refs": [{"label": f"{c} limit", "value": lim[c].get("mem_limit")} for c in containers if lim[c].get("mem_limit")],
        },
        {
            "id": "memcomp", "title": ("Job" if xkeys else "Pod") + " memory: what the working set is made of", "unit": "bytes", "stacked": True,
            "sub": "Anonymous (heap/stack, can't be reclaimed) + active page cache & kernel, with reclaimable cache on top. Only the bottom two count toward OOM.",
            "series": [
                {"name": "anonymous", "seq": 600, "values": S("pod:anon")},
                {"name": "active cache + kernel", "seq": 400, "values": S("pod:other")},
                {"name": "reclaimable cache", "seq": 200, "values": S("pod:inact")},
            ],
        },
        {
            "id": "cpu", "title": "CPU by container", "unit": "cores",
            "sub": "Cores in use, averaged per sample interval.",
            "series": [{"name": disp[c], "slot": slot_for(disp[c], names), "values": S(f"cpu:{c}")} for c in chart_keys] + job_cpu_line,
            "refs": [{"label": f"{c} limit", "value": lim[c].get("cpu_limit")} for c in containers if lim[c].get("cpu_limit")],
        },
        {
            "id": "thr", "title": "CPU throttling by container", "unit": "pct",
            "sub": "Share of 100 ms CFS periods in which the container hit its CPU limit and was paused (5 s rolling average).",
            "series": [{"name": disp[c], "slot": slot_for(disp[c], names), "values": S(f"thr:{c}")} for c in containers if lim[c].get("cpu_limit")],
        },
        {
            "id": "kind", "title": "Inside the kind cluster: memory by workload", "unit": "bytes", "stacked": True,
            "sub": "Working set of the E2E cluster's pods, grouped by Deployment/StatefulSet/DaemonSet (replicas summed). Top 7 by peak; the rest are grouped as “other”.",
            "series": ([{"name": wl, "slot": i + 1, "values": S(f"wl:{wl}")} for i, wl in enumerate(top_wl)]
                       + ([{"name": f"other ({len(rest)})", "gray": True, "values": S("wl:__other")}] if rest else [])),
        } if kind_rows else None,
        {
            "id": "psi", "title": "Stall time — this pod", "unit": "pct",
            "sub": "Pressure stall information: share of time at least one task in the pod was waiting on memory reclaim or for a CPU.",
            "series": [
                {"name": "memory stall", "ctx": "mem", "values": S("pod:mpsi")},
                {"name": "CPU stall", "ctx": "cpu", "values": S("pod:cpsi")},
            ],
        },
        {
            "id": "node", "title": "Whole node — contention context", "unit": "pct",
            "sub": "Host memory in use and CPU busy, all workloads. High values here with low values above mean the job was squeezed by neighbours, not by itself.",
            "series": [
                {"name": "node memory used", "ctx": "mem", "values": S("node:mem_used_pct")},
                {"name": "node CPU busy", "ctx": "cpu", "values": S("node:cpu_busy_pct")},
            ],
        },
        {
            "id": "nodepsi", "title": "Stall time — whole node", "unit": "pct",
            "sub": "Host-wide pressure stalls.",
            "series": [
                {"name": "memory stall", "ctx": "mem", "values": S("node:mpsi")},
                {"name": "CPU stall", "ctx": "cpu", "values": S("node:cpsi")},
            ],
        },
    ]
    charts = [c for c in charts if c and c["series"]]

    env = os.environ
    return {
        "job": job_name or env.get("GITHUB_JOB") or meta.get("pod"),
        "repo": env.get("GITHUB_REPOSITORY"),
        "workflow": env.get("GITHUB_WORKFLOW"),
        "run_url": f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
        if env.get("GITHUB_REPOSITORY") and env.get("GITHUB_RUN_ID") else None,
        "sha": (env.get("GITHUB_SHA") or "")[:10] or None,
        "meta": meta,
        "t_start": t_start,
        "t_end": t_end,
        "n_samples": len(samples),
        "containers": names,
        "rows": rows,
        "kind": kind_rows,
        "steps": step_rows,
        "chart_data": {
            "t": [round(x, 1) for x in dt],
            "t0": t_start,
            "steps": [{"name": r["name"], "start": r["start"], "end": r["start"] + r["dur"]} for r in step_rows],
            "charts": charts,
        },
        "raw": (t, s, node, chart_keys + ["pod"] + (["job"] if xkeys else []), disp, wl_series),
    }


def slot_for(c, containers):
    """Colour follows the container, never its rank: a small-pool report (runner only) and a
    large-pool report paint `runner` the same blue."""
    if c in KNOWN_ORDER:
        return KNOWN_ORDER.index(c) + 1
    return min(8, len(KNOWN_ORDER) + 1 + sorted(x for x in containers if x not in KNOWN_ORDER).index(c))


# ------------------------------------------------------------------ outputs


def write_csv(rep, path):
    t, s, node, keys, disp, wl_series = rep["raw"]
    cols = []
    for c in keys:
        name = re.sub(r"\W+", "_", disp[c].replace("dind › ", "dind_")).strip("_")
        for f in ("ws", "anon", "file", "inact", "cpu", "thr", "mpsi", "cpsi"):
            if f in s[c]:
                cols.append((f"{name}_{f}", s[c][f]))
    for wl, (ws, cpu) in sorted(wl_series.items()):
        name = re.sub(r"\W+", "_", wl)
        cols += [(f"kind_{name}_ws", ws), (f"kind_{name}_cpu", cpu)]
    for f, xs in node.items():
        cols.append((f"node_{f}", xs))
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "elapsed_s"] + [n for n, _ in cols])
        for i, tt in enumerate(t):
            w.writerow(
                [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tt)), round(tt - rep["t_start"], 1)]
                + ["" if xs[i] is None else (round(xs[i], 4) if isinstance(xs[i], float) else xs[i]) for _, xs in cols]
            )


def summary_md(rep, artifact_hint=True):
    m = rep["meta"]
    L = [f"### Runner resources — {rep['job']}", ""]
    L.append(f"`{m.get('pod')}` · pool `{m.get('pool')}` · {fmt_dur(rep['t_end'] - rep['t_start'])} · {rep['n_samples']} samples @ {m.get('interval_s', 1)}s")
    L.append("")
    L.append("| container | mem peak | p95 | limit | peak % | OOM kills | CPU avg | CPU peak (5s) | CPU limit | throttled |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for r in rep["rows"]:
        flag = " ⚠" if (r["ws_peak_pct"] or 0) >= 85 or r["oom_kills"] else ""
        L.append(
            f"| {r['name']} | {fmt_bytes(r['ws_peak'])}{flag} | {fmt_bytes(r['ws_p95'])} | {fmt_bytes(r['mem_limit'])} "
            f"| {fmt_pct(r['ws_peak_pct'])} | {r['oom_kills']} | {fmt_cores(r['cpu_avg'])} | {fmt_cores(r['cpu_peak'])} "
            f"| {fmt_cores(r['cpu_limit'])} | {fmt_pct(r['throttled_pct'])} |"
        )
    if rep["kind"]:
        L += ["", f"**Inside the kind cluster** — top workloads by peak working set ({len(rep['kind'])} total)", "",
              "| workload | pods | mem peak | p95 | CPU avg | CPU peak (5s) | OOM kills |", "|---|--:|--:|--:|--:|--:|--:|"]
        for k in rep["kind"][:10]:
            L.append(f"| {k['name']} | {k['pods']} | {fmt_bytes(k['ws_peak'])} | {fmt_bytes(k['ws_p95'])} | "
                     f"{fmt_cores(k['cpu_avg'])} | {fmt_cores(k['cpu_peak'])} | {k['oom_kills']} |")
    top = sorted((x for x in rep["steps"] if x.get("ws_peak")), key=lambda x: -x["ws_peak"])[:5]
    if top:
        L += ["", "**Heaviest steps by memory**", "", "| step | duration | mem peak | CPU avg | CPU peak (5s) |", "|---|--:|--:|--:|--:|"]
        for x in top:
            L.append(f"| {x['i']}. {x['name']} | {fmt_dur(x['dur'])} | {fmt_bytes(x['ws_peak'])} | {fmt_cores(x['cpu_avg'])} | {fmt_cores(x['cpu_peak'])} |")
    if artifact_hint:
        L += ["", "<sub>Charts: the `resource-report-*` artifact, if this job uses the "
              "`ergonlabs/gh-actions-k8s-runner/resource-report` action. Data: sampled per container from the host's cgroups.</sub>"]
    return "\n".join(L) + "\n"


def stdout_table(rep):
    out = [f"resource report: {rep['job']} ({fmt_dur(rep['t_end'] - rep['t_start'])}, {rep['n_samples']} samples)"]
    out.append(f"  {'container':<22}{'mem peak':>11}{'limit':>11}{'peak%':>7}{'oom':>5}{'cpu avg':>9}{'cpu pk':>8}{'thr':>6}")
    for r in rep["rows"]:
        out.append(
            f"  {r['name']:<22}{fmt_bytes(r['ws_peak']):>11}{fmt_bytes(r['mem_limit']):>11}{fmt_pct(r['ws_peak_pct']):>7}"
            f"{r['oom_kills']:>5}{fmt_cores(r['cpu_avg']):>9}{fmt_cores(r['cpu_peak']):>8}{fmt_pct(r['throttled_pct']):>6}"
        )
    for k in rep["kind"][:10]:
        out.append(f"    kind {k['name']:<22}{fmt_bytes(k['ws_peak']):>11}  cpu avg {fmt_cores(k['cpu_avg'])}")
    return "\n".join(out)


def tile(label, value, note="", status=None):
    st = f' data-status="{status}"' if status else ""
    icon = {"critical": "✕ ", "warning": "! "}.get(status, "")
    return (f'<div class="tile"{st}><div class="tl">{html.escape(label)}</div>'
            f'<div class="tv">{html.escape(value)}</div>'
            f'<div class="tn">{icon}{html.escape(note)}</div></div>')


def render_html(rep):
    m = rep["meta"]
    totals = [r for r in rep["rows"] if r["total"]]
    pod, job = totals[0], totals[-1]  # job == pod when nothing ran outside the pod's cgroups
    nested = [r for r in rep["rows"] if r["nested"]]
    worst = max((r for r in rep["rows"] if not r["total"] and r["ws_peak_pct"] is not None),
                key=lambda r: r["ws_peak_pct"], default=None)
    ooms = sum(r["oom_kills"] for r in rep["rows"] if not r["total"])
    e = html.escape

    def mem_status(p):
        return "critical" if p is not None and p >= 90 else "warning" if p is not None and p >= 75 else None

    tiles = [
        tile("Peak job memory", fmt_bytes(job["ws_peak"]),
             f"k8s pod {fmt_bytes(pod['ws_peak'])} + dind-spawned {fmt_bytes(sum(r['ws_peak'] or 0 for r in nested))} (peaks)" if nested
             else f"{fmt_pct(pod['ws_peak_pct'])} of {fmt_bytes(pod['mem_limit'])} pod limit" if pod["mem_limit"] else "no pod limit"),
    ] + ([
        tile("Outside k8s limits", fmt_bytes(max((r["ws_peak"] or 0) for r in nested)),
             "largest dind-spawned container — no memory limit applies", "warning"),
    ] if nested else []) + [
        tile("Closest to its limit", f"{worst['name']} · {fmt_pct(worst['ws_peak_pct'])}" if worst else "–",
             f"{fmt_bytes(worst['ws_peak'])} of {fmt_bytes(worst['mem_limit'])}" if worst else "",
             mem_status(worst["ws_peak_pct"]) if worst else None),
        tile("OOM kills", str(ooms), "none in this job" if not ooms else "a process was killed for memory",
             "critical" if ooms else None),
        tile("Avg CPU", f"{fmt_cores(job['cpu_avg'])} cores",
             f"peak {fmt_cores(job['cpu_peak'])} (5 s avg) of {fmt_cores(job['cpu_limit'])}" if job["cpu_limit"] else f"peak {fmt_cores(job['cpu_peak'])}"),
        tile("CPU throttled", fmt_pct(pod["throttled_pct"]), "of CFS periods, pod-wide",
             "warning" if (pod["throttled_pct"] or 0) >= 10 else None),
        tile("Duration", fmt_dur(rep["t_end"] - rep["t_start"]), f"{rep['n_samples']} samples @ {m.get('interval_s', 1)} s"),
    ]

    head = ["container", "mem request", "mem limit", "peak", "p95", "median", "peak % of limit", "anon peak",
            "OOM kills", "hit limit", "CPU request", "CPU limit", "CPU avg", "CPU p95", "CPU peak", "CPU-seconds", "throttled"]
    trs = []
    for r in rep["rows"]:
        cls = ' class="total"' if r["total"] else ""
        sw = "" if r["total"] else f'<span class="sw" style="background:var(--s{slot_for(r["name"], rep["containers"])})"></span>'
        trs.append(
            f"<tr{cls}><th scope=row>{sw}{e(r['name'])}</th><td>{fmt_bytes(r['mem_request'])}</td><td>{fmt_bytes(r['mem_limit'])}</td>"
            f"<td><b>{fmt_bytes(r['ws_peak'])}</b></td><td>{fmt_bytes(r['ws_p95'])}</td><td>{fmt_bytes(r['ws_p50'])}</td>"
            f"<td>{fmt_pct(r['ws_peak_pct'])}</td><td>{fmt_bytes(r['anon_peak'])}</td><td>{r['oom_kills']}</td><td>{r['mem_max_events']}</td>"
            f"<td>{fmt_cores(r['cpu_request'])}</td><td>{fmt_cores(r['cpu_limit'])}</td><td>{fmt_cores(r['cpu_avg'])}</td>"
            f"<td>{fmt_cores(r['cpu_p95'])}</td><td>{fmt_cores(r['cpu_peak'])}</td><td>{r['cpu_seconds']:.0f}</td><td>{fmt_pct(r['throttled_pct'])}</td></tr>"
        )
    ctable = ("<table><thead><tr>" + "".join(f"<th scope=col>{h}</th>" for h in head) + "</tr></thead><tbody>"
              + "".join(trs) + "</tbody></table>")

    krows = []
    for k in rep["kind"]:
        krows.append(
            f"<tr><th scope=row>{e(k['name'])}</th><td>{k['pods']}</td><td><b>{fmt_bytes(k['ws_peak'])}</b></td>"
            f"<td>{fmt_bytes(k['ws_p95'])}</td><td>{fmt_bytes(k['ws_p50'])}</td><td>{fmt_bytes(k['anon_peak'])}</td>"
            f"<td>{k['oom_kills']}</td><td>{fmt_cores(k['cpu_avg'])}</td><td>{fmt_cores(k['cpu_peak'])}</td><td>{k['cpu_seconds']:.0f}</td></tr>"
        )
    ktable = ("<h2>Inside the kind cluster</h2><div class=\"muted\" style=\"font-size:12px;margin-bottom:8px\">Every pod the E2E "
              "kind cluster ran, grouped by workload (replicas summed at each second). Sampled from the kind node's nested cgroups; "
              "no Kubernetes limit on the runner pod applies to any of these.</div><div class=\"scroll\"><table><thead><tr>"
              + "".join(f"<th scope=col>{h}</th>" for h in ["workload", "pods", "mem peak", "p95", "median", "anon peak", "OOM kills",
                                                           "CPU avg", "CPU peak (5 s)", "CPU-seconds"])
              + "</tr></thead><tbody>" + "".join(krows) + "</tbody></table></div>") if krows else ""

    srows = []
    for x in rep["steps"]:
        srows.append(
            f"<tr><td>{x['i']}</td><th scope=row>{e(x['name'])}</th><td>{fmt_dur(x['start'])}</td><td>{fmt_dur(x['dur'])}</td>"
            f"<td><b>{fmt_bytes(x.get('ws_peak'))}</b></td><td>{e(x.get('top') or '')}</td><td>{fmt_cores(x.get('cpu_avg'))}</td>"
            f"<td>{fmt_cores(x.get('cpu_peak'))}</td><td>{fmt_pct(x.get('thr'))}</td><td>{fmt_pct(x.get('mpsi'))}</td></tr>"
        )
    stable = ("<table><thead><tr><th scope=col>#</th><th scope=col>step</th><th scope=col>starts at</th><th scope=col>duration</th>"
              "<th scope=col>mem peak</th><th scope=col>largest container</th><th scope=col>CPU avg</th><th scope=col>CPU peak (5 s)</th>"
              "<th scope=col>throttled peak</th><th scope=col>mem stall peak</th></tr></thead><tbody>" + "".join(srows) + "</tbody></table>"
              ) if srows else "<p class=muted>No step markers found (runner _diag log not available).</p>"

    ctx = []
    if rep["repo"]:
        ctx.append(e(rep["repo"]))
    if rep["workflow"]:
        ctx.append(e(rep["workflow"]))
    if rep["sha"]:
        ctx.append(f"<code>{e(rep['sha'])}</code>")
    if rep["run_url"]:
        ctx.append(f'<a href="{e(rep["run_url"])}">workflow run</a>')
    ctx.append(f"runner <code>{e(m.get('pod', ''))}</code> · pool <code>{e(str(m.get('pool')))}</code> · node <code>{e(str(m.get('node')))}</code>")
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(rep["t_start"])) + " → " + time.strftime("%H:%M:%S UTC", time.gmtime(rep["t_end"]))

    data = json.dumps(rep["chart_data"], separators=(",", ":")).replace("</", "<\\/")
    return (TEMPLATE
            .replace("{{TITLE}}", e(f"Resources · {rep['job']}"))
            .replace("{{JOB}}", e(rep["job"]))
            .replace("{{CTX}}", " · ".join(ctx))
            .replace("{{WHEN}}", e(when))
            .replace("{{TILES}}", "".join(tiles))
            .replace("{{CTABLE}}", ctable)
            .replace("{{KTABLE}}", ktable)
            .replace("{{STABLE}}", stable)
            .replace("{{DATA}}", data))


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root{color-scheme:light;
 --page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--band:rgba(11,11,11,.035);--ring:rgba(11,11,11,.10);
 --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;--s8:#e34948;
 --q200:#9ec5f4;--q400:#3987e5;--q600:#184f95;--mem:#4a3aa7;--cpu:#008300;--warn:#fab219;--crit:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--band:rgba(255,255,255,.035);--ring:rgba(255,255,255,.10);
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;
 --q200:#184f95;--q400:#2a78d6;--q600:#86b6ef;--mem:#9085e9;--cpu:#0ca30c}}
:root[data-theme=dark]{color-scheme:dark;
 --page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--band:rgba(255,255,255,.035);--ring:rgba(255,255,255,.10);
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;
 --q200:#184f95;--q400:#2a78d6;--q600:#86b6ef;--mem:#9085e9;--cpu:#0ca30c}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px;font-weight:650}
h2{font-size:15px;margin:36px 0 10px;font-weight:650}
.ctx,.muted{color:var(--ink2)} .ctx{font-size:13px} code{font-size:12px}
a{color:var(--s1)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-top:20px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px 14px}
.tl{font-size:12px;color:var(--ink2)} .tv{font-size:20px;font-weight:600;margin:2px 0} .tn{font-size:12px;color:var(--muted)}
.tile[data-status=warning]{border-left:4px solid var(--warn)} .tile[data-status=critical]{border-left:4px solid var(--crit)}
.tile[data-status] .tn{color:var(--ink2)}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px 8px;margin-top:12px;position:relative}
.card h3{font-size:14px;margin:0;font-weight:600} .card .sub{font-size:12px;color:var(--ink2);margin:2px 0 6px}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12px;color:var(--ink2);margin:4px 0}
.legend span{display:inline-flex;align-items:center;gap:6px} .legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
.legend i.box{height:10px;width:10px;border-radius:2px}
svg{display:block;width:100%;overflow:visible}
svg text{font-size:11px;fill:var(--muted);font-variant-numeric:tabular-nums}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--ring);box-shadow:0 4px 16px rgba(0,0,0,.12);
 border-radius:8px;padding:8px 10px;font-size:12px;min-width:170px;z-index:5}
.tip b{font-weight:600} .tip .r{display:flex;justify-content:space-between;gap:14px;font-variant-numeric:tabular-nums}
.tip .k{display:inline-flex;align-items:center;gap:6px;color:var(--ink2)} .tip i{width:10px;height:3px;border-radius:2px;display:inline-block}
.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--ring);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--grid)}
thead th{color:var(--ink2);font-weight:500;background:var(--surface);position:sticky;top:0}
tbody th,thead th:nth-child(-n+2){text-align:left;font-weight:500} td:first-child{text-align:right;color:var(--muted)}
tr.total th,tr.total td{font-weight:600;border-top:1px solid var(--axis)}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px;vertical-align:-1px}
footer{margin-top:36px;font-size:12px;color:var(--muted)}
</style></head><body><main>
<h1>{{JOB}}</h1>
<div class="ctx">{{CTX}}</div>
<div class="ctx">{{WHEN}}</div>
<div class="tiles">{{TILES}}</div>
<h2>Over time</h2>
<div class="muted" style="font-size:12px">Shaded bands are workflow steps, numbered as in the per-step table below. Hover any chart for exact values; the crosshair is shared across charts.</div>
<div id="charts"></div>
<h2>Per container</h2>
<div class="scroll">{{CTABLE}}</div>
{{KTABLE}}
<h2>Per step</h2>
<div class="scroll">{{STABLE}}</div>
<footer>Sampled once per second per container from the host's cgroup v2 counters by arc-resource-sampler (ergonlabs/gh-actions-k8s-runner).
"Memory" is the working set (memory.current − inactive_file). "Hit limit" counts memory.events max — times the container was forced into reclaim at its limit.
CPU peaks use a 5 s rolling average. Raw series: resource-samples.csv.</footer>
</main>
<script id="data" type="application/json">{{DATA}}</script>
<script>
(function(){
const D=JSON.parse(document.getElementById('data').textContent);
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const colorOf=s=>s.slot?`var(--s${s.slot})`:s.seq?`var(--q${s.seq})`:s.ink?'var(--ink2)':s.gray?'var(--muted)':`var(--${s.ctx})`;
const GiB=2**30,MiB=2**20;
const fmt={bytes:v=>v==null?'–':v>=GiB?(v/GiB).toFixed(v>=10*GiB?1:2)+' GiB':(v/MiB).toFixed(0)+' MiB',
 cores:v=>v==null?'–':v.toFixed(2),pct:v=>v==null?'–':(v>=10?v.toFixed(0):v.toFixed(1))+'%'};
const axisFmt={bytes:v=>v===0?'0':v>=GiB?(+(v/GiB).toFixed(1))+' GiB':(v/MiB).toFixed(0)+' MiB',cores:v=>+v.toFixed(2)+'',pct:v=>v+'%'};
const el=(t,a={},p)=>{const e=document.createElementNS('http://www.w3.org/2000/svg',t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e};
const mmss=s=>{s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}`:`${m}:${String(x).padStart(2,'0')}`};
function nice(max,n){if(!(max>0))return[0,1];const raw=max/n,p=10**Math.floor(Math.log10(raw)),f=raw/p;const st=(f<=1?1:f<=2?2:f<=2.5?2.5:f<=5?5:10)*p;return[st,Math.ceil(max/st)*st]}
function niceBytes(max){const u=max>=GiB?GiB:MiB;const[st,top]=nice(max/u,4);return[st*u,top*u]}
function niceTime(max){const c=[5,10,15,30,60,120,300,600,900,1800,3600,7200];for(const s of c)if(max/s<=8)return s;return 14400}
const T=D.t,N=T.length,tMax=T[N-1]||1;
const cards=[],root=document.getElementById('charts');
const hover={i:null};
function stepAt(x){for(const s of D.steps)if(x>=s.start&&x<s.end)return s;return null}
function draw(){
 root.innerHTML='';cards.length=0;
 for(const c of D.charts){
  const card=document.createElement('div');card.className='card';root.appendChild(card);
  card.innerHTML=`<h3>${c.title}</h3><div class="sub">${c.sub||''}</div>`;
  if(c.series.length>1){const lg=document.createElement('div');lg.className='legend';
   for(const s of c.series){const pk=Math.max(...s.values.filter(v=>v!=null),0);
    lg.insertAdjacentHTML('beforeend',`<span><i class="${c.stacked?'box':''}" style="background:${colorOf(s)}"></i>${s.name}<span style="color:var(--muted)">${c.unit==='bytes'||c.unit==='cores'?'peak '+fmt[c.unit](pk):''}</span></span>`)}
   card.appendChild(lg)}
  const W=Math.max(320,card.clientWidth-32),H=c.id==='mem'?250:190,m={l:64,r:84,t:8,b:22},iw=W-m.l-m.r,ih=H-m.t-m.b;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,height:H,role:'img','aria-label':c.title},card);
  // data max (stacked sums)
  let dmax=0;const cum=[];
  if(c.stacked){for(let i=0;i<N;i++){let a=0;cum.push(c.series.map(s=>{a+=s.values[i]||0;return a}));dmax=Math.max(dmax,a)}}
  else for(const s of c.series)for(const v of s.values)if(v!=null&&v>dmax)dmax=v;
  const refs=(c.refs||[]).filter(r=>r.value&&r.value<=Math.max(dmax,1e-9)*1.6);
  const offscale=(c.refs||[]).filter(r=>r.value&&!refs.includes(r));
  let top=Math.max(dmax,...refs.map(r=>r.value))*1.05;
  if(c.unit==='pct')top=Math.max(top,5);
  if(c.unit==='cores')top=Math.max(top,0.5);
  const [ys,ymax]=c.unit==='bytes'?niceBytes(top):c.unit==='pct'&&top>60?[25,100]:nice(top,4);
  const X=t=>m.l+t/tMax*iw,Y=v=>m.t+ih-v/ymax*ih;
  // step bands
  D.steps.forEach((s,i)=>{const w=Math.max(0,X(Math.min(s.end,tMax))-X(s.start));if(i%2)el('rect',{x:X(s.start),y:m.t,width:w,height:ih,fill:'var(--band)'},svg);
   if(c===D.charts[0]&&w>=16){const tx=el('text',{x:X(s.start)+w/2,y:m.t+12,'text-anchor':'middle'},svg);tx.textContent=i+1}});
  // grid + y axis
  for(let v=0;v<=ymax+1e-9;v+=ys){el('line',{x1:m.l,x2:m.l+iw,y1:Y(v),y2:Y(v),stroke:v?'var(--grid)':'var(--axis)','stroke-width':1},svg);
   const tx=el('text',{x:m.l-8,y:Y(v)+4,'text-anchor':'end'},svg);tx.textContent=axisFmt[c.unit](+v.toFixed(6))}
  const xs=niceTime(tMax);for(let t=0;t<=tMax;t+=xs){const tx=el('text',{x:X(t),y:H-4,'text-anchor':'middle'},svg);tx.textContent=mmss(t)}
  // series
  const path=(vals,base)=>{let d='',pen=false;for(let i=0;i<N;i++){const v=vals[i];if(v==null){pen=false;continue}d+=(pen?'L':'M')+X(T[i]).toFixed(1)+' '+Y(v).toFixed(1);pen=true}return d};
  if(c.stacked){for(let k=c.series.length-1;k>=0;k--){let d='';let lo=[];
    for(let i=0;i<N;i++){d+=(i?'L':'M')+X(T[i]).toFixed(1)+' '+Y(cum[i][k]).toFixed(1)}
    for(let i=N-1;i>=0;i--){d+='L'+X(T[i]).toFixed(1)+' '+Y(k?cum[i][k-1]:0).toFixed(1)}
    el('path',{d:d+'Z',fill:colorOf(c.series[k]),stroke:'var(--surface)','stroke-width':1},svg)}}
  else for(const s of c.series){el('path',{d:path(s.values),fill:'none',stroke:colorOf(s),'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'},svg)}
  // limit reference lines, labelled at the right edge
  const used=[];for(const r of refs.sort((a,b)=>b.value-a.value)){if(r.value<ymax*0.05)continue;let y=Y(r.value);
   el('line',{x1:m.l,x2:m.l+iw,y1:y,y2:y,stroke:'var(--ink2)','stroke-width':1,opacity:.55},svg);
   let ly=y+4;while(used.some(u=>Math.abs(u-ly)<12))ly+=12;used.push(ly);
   const tx=el('text',{x:m.l+iw+6,y:ly},svg);const nm=r.label.replace(' limit','');tx.textContent=(nm.length>11?nm.slice(0,10)+'…':nm)+' limit'}
  if(offscale.length){const n=document.createElement('div');n.className='legend';n.style.color='var(--muted)';
   n.textContent='Off-scale limits (usage < 60%): '+offscale.map(r=>r.label.replace(' limit','')+' '+fmt[c.unit](r.value)).join(', ');card.appendChild(n)}
  // hover layer
  const cross=el('line',{y1:m.t,y2:m.t+ih,stroke:'var(--ink2)','stroke-width':1,visibility:'hidden'},svg);
  const dots=c.stacked?[]:c.series.map(s=>el('circle',{r:4,fill:colorOf(s),stroke:'var(--surface)','stroke-width':2,visibility:'hidden'},svg));
  const hit=el('rect',{x:m.l,y:0,width:iw,height:H,fill:'transparent'},svg);
  const tip=document.createElement('div');tip.className='tip';tip.hidden=true;card.appendChild(tip);
  const obj={c,svg,cross,dots,tip,X,Y,W,m,iw,cum};cards.push(obj);
  const move=ev=>{const r=svg.getBoundingClientRect();const x=(ev.clientX-r.left)/r.width*W;const t=(x-m.l)/iw*tMax;
   let lo=0,hi=N-1;while(lo<hi){const mid=(lo+hi)>>1;if(T[mid]<t)lo=mid+1;else hi=mid}
   if(lo>0&&Math.abs(T[lo-1]-t)<Math.abs(T[lo]-t))lo--;hover.i=lo;hover.card=obj;sync()};
  hit.addEventListener('pointermove',move);hit.addEventListener('pointerdown',move);
  hit.addEventListener('pointerleave',()=>{hover.i=null;sync()});
 }
}
function sync(){
 for(const o of cards){const i=hover.i;const show=i!=null;
  o.cross.setAttribute('visibility',show?'visible':'hidden');
  o.dots.forEach(d=>d.setAttribute('visibility','hidden'));
  if(!show){o.tip.hidden=true;continue}
  const x=o.X(T[i]);o.cross.setAttribute('x1',x);o.cross.setAttribute('x2',x);
  o.c.series.forEach((s,k)=>{const v=s.values[i];if(!o.dots[k]||v==null)return;o.dots[k].setAttribute('cx',x);o.dots[k].setAttribute('cy',o.Y(v));o.dots[k].setAttribute('visibility','visible')});
  if(hover.card!==o){o.tip.hidden=true;continue}
  const st=stepAt(T[i]);const clock=new Date((D.t0+T[i])*1000).toISOString().slice(11,19);
  let h=`<div><b>${mmss(T[i])}</b> <span style="color:var(--muted)">${clock} UTC</span></div>`;
  if(st)h+=`<div style="color:var(--ink2);margin-bottom:4px">${st.name.replace(/</g,'&lt;')}</div>`;
  const rows=o.c.series.map(s=>({s,v:s.values[i]}));if(!o.c.stacked)rows.sort((a,b)=>(b.v??-1)-(a.v??-1));
  for(const {s,v} of rows)h+=`<div class="r"><span class="k"><i style="background:${colorOf(s)}"></i>${s.name}</span><span>${fmt[o.c.unit](v)}</span></div>`;
  if(o.c.stacked){const tot=(o.c.series[0].values[i]||0)+(o.c.series[1].values[i]||0);h+=`<div class="r" style="border-top:1px solid var(--grid);margin-top:3px;padding-top:3px"><span class="k">working set</span><span>${fmt.bytes(tot)}</span></div>`}
  o.tip.innerHTML=h;o.tip.hidden=false;
  const cw=o.svg.parentNode.clientWidth,px=o.svg.offsetLeft+x/o.W*o.svg.clientWidth;
  o.tip.style.left=(px+14+190>cw?px-14-o.tip.offsetWidth:px+14)+'px';o.tip.style.top=(o.svg.offsetTop+8)+'px'}
}
draw();let rt;addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{draw();sync()},150)});
})();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--metrics-dir", default=os.environ.get("RESOURCE_METRICS_DIR", "/resource-metrics"))
    ap.add_argument("--diag-dir", default=os.environ.get("RUNNER_DIAG_DIR", "/home/runner/_diag"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", default="", help="append a markdown digest to this file (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--no-artifact-hint", action="store_true")
    a = ap.parse_args()

    rep = build(a.metrics_dir, a.diag_dir)
    if rep is None:
        print(f"resource-report: no samples in {a.metrics_dir} — not an arc-resource-sampler runner pod?", file=sys.stderr)
        return 3
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "resource-report.html"), "w") as f:
        f.write(render_html(rep))
    write_csv(rep, os.path.join(a.out, "resource-samples.csv"))
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(summary_md(rep, artifact_hint=not a.no_artifact_hint))
    print(stdout_table(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
