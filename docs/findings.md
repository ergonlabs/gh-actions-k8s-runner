# Findings

Every non-obvious thing that broke while building this, with the evidence. Each one cost
real debugging time. **If you are tempted to "simplify" something in the manifests, look for
it here first** — most of the odd-looking choices are load-bearing.

All measurements were taken on the host described in `prerequisites.md` (Ubuntu 22.04,
kernel 5.15.0-186, k3s v1.35.5+k3s1, runner 2.336.0) on 2026-08-13.

---

## 1. overlayfs cannot use an NFS upperdir — engine stores need a block device

**Symptom.** `dockerd` starts happily with `--data-root` on NFS, then every build fails:

```
ERROR: failed to solve: process "/bin/sh -c echo hello > /x" did not complete successfully:
mount source: "overlay", target: ".../cachemounts/buildkit1051850749", fstype: overlay,
data: "workdir=/var/lib/docker/.../snapshots/4/work,upperdir=.../snapshots/4/fs,...",
err: invalid argument
```

**Cause.** The kernel refuses an NFS directory as an overlayfs `upperdir`. This is not a
speed problem and no amount of NFS tuning fixes it. BuildKit has the identical constraint —
its default snapshotter is also overlayfs — so "just use BuildKit" does not dodge it.

**Fix.** Engine stores (`/var/lib/docker`, `/var/lib/buildkit`) live on a real block device.
Here that's an iSCSI LUN from the NAS, ext4, mounted at `/mnt/arc-block` (`host/setup-iscsi.sh`).

**Do not** point any engine store at NFS. If you have no block device available, this whole
design does not work; get one first.

---

## 2. The chart's `containerMode: dind` hardcodes its sidecar

**Symptom.** You cannot relocate `/var/lib/docker` through Helm values, no matter what you put
in `template.spec.containers`.

**Cause.** `gha-runner-scale-set/templates/_helpers.tpl` defines the dind container inline —
image, args and `volumeMounts` are fixed — and `non-runner-non-dind-containers` explicitly
*filters out* any user-supplied container named `dind`. Your override is silently discarded.

**Fix.** Leave `containerMode` **unset**. In default mode the chart passes `initContainers`,
`containers` and `volumes` through verbatim, so `manifests/20-scale-set.yaml` hand-rolls the
dind sidecar — a faithful copy of the chart's, plus the storage overrides.

Consequences of hand-rolling, all handled in the manifest:
- `dind` and `buildkitd` are `initContainers` with `restartPolicy: Always` (native sidecars,
  needs k8s ≥ 1.29).
- Per-pod isolation comes from `subPathExpr: $(POD_NAME)` on every shared hostPath, which
  needs `POD_NAME` from a `fieldRef` in **every** container that mounts one.
- The runner container and `init-dind-externals` **must use the same image** — the init
  container copies `/home/runner/externals` out of it.

---

## 3. The stock runner image lacks `libatomic1` — all Node jobs die

**Symptom.**

```
/home/runner/_work/_tool/node/26.1.0/x64/bin/node: error while loading shared libraries:
libatomic.so.1: cannot open shared object file: No such file or directory
```

Two *different-looking* failures from this one cause: `pnpm install` exits 127, and elsewhere
`Get pnpm store directory` returns empty output so `actions/cache` fails with
`Input required and not supplied: path`.

**Cause.** `ghcr.io/actions/actions-runner` is deliberately minimal. Node 26 (what
`actions/setup-node` installs) links against `libatomic.so.1`.

**Fix.** `runner-image/Dockerfile` installs it. Note there is **no drop-in alternative**:
GitHub's real `ubuntu-latest` comes from `actions/runner-images` and is a ~30 GB *VM* image,
not a container. Extending the official ARC runner image is the supported path.

---

## 4. Go must be in the image, even though a `setup-go` job exists

**Symptom.** One test fails out of 351: `go vet ./...` → `sh: 1: go: not found`.

**Cause.** `ubuntu-latest` has `go` on `PATH` by default, and workflows rely on that even
when they do not run `actions/setup-go`. A cross-workspace `pnpm -r typecheck` shelled into a
Go package's typecheck script. The *dedicated* Go job passed, because it does use
`actions/setup-go` — only the cross-workspace path depended on the default toolchain.

**Fix.** Install Go in the image, pinned to the newest version the hosted image caches.

**General principle:** anything `ubuntu-latest` has preinstalled is something a workflow may
assume. Check `actions/runner-images/images/ubuntu/Ubuntu2404-Readme.md` and match versions.
That's why helm/kubectl/kustomize/kind/yq are pinned to the hosted image's versions rather
than "latest" — notably **helm 3.x, not 4.x**.

> ⚠️ Installing a tool in the image can *break* the corresponding `setup-*` action. See
> **finding #14** — the first attempt at this shipped `ENV GOROOT` and broke every job that
> used `actions/setup-go`.

---

## 5. buildkitd's socket is root-only; the runner is uid 1001

**Symptom.** `docker buildx build --builder remote` hangs then fails with
`context deadline exceeded`; `docker buildx inspect` shows the builder as `inactive`.

**Cause.** buildkitd creates `srw-rw---- root root /run/buildkit/buildkitd.sock`. The runner
process is uid 1001 and cannot open it. buildkitd itself is perfectly healthy — its log shows
a registered worker — so nothing looks wrong on the daemon side.

**Fix.** `--group=123` on buildkitd. 123 is the `docker` group the runner image already puts
uid 1001 into, matching `DOCKER_GROUP_GID` on the dind sidecar. Socket becomes
`srw-rw---- root docker`.

Also note **`buildctl` is not in the runner image** — it lives in the buildkit sidecar. From
the runner you reach BuildKit via buildx's remote driver:

```yaml
- run: docker buildx create --name arc --driver remote unix:///run/buildkit/buildkitd.sock --use
- run: docker buildx build --push -t <registry>/img:tag .
```

The remote driver keeps results in cache only — pass `--push` or `--load` or the image goes
nowhere (buildx warns but still exits 0).

---

## 6. The runner overwrites `ACTIONS_RESULTS_URL` — the cache server needs a binary patch

**Symptom.** You set `ACTIONS_RESULTS_URL` on the pod, confirm it with `printenv`, and
`actions/cache` still uses GitHub's hosted cache. The self-hosted cache server logs **zero**
requests while a job cheerfully restores a 207 MB cache from GitHub.

**Cause.** The runner injects the cache URLs from the job message it receives from GitHub,
overriding the container environment.

**Fix.** Rename the string inside `Runner.Worker.dll` so the runner writes a variable nobody
reads: `ACTIONS_RESULTS_URL` → `ACTIONS_RESULTS_ORL` (same byte length, assembly stays valid).
Our value then survives.

**The trap within the trap.** Upstream documents this as a `sed` on the DLL. **That silently
does nothing here** — on runner 2.336.0 the literal is stored as **UTF-16LE**, so an ASCII
`sed` matches zero bytes and produces a normal-looking, unpatched image. The Dockerfile does
an explicit UTF-16LE byte replacement and **asserts exactly one occurrence**, so a future
runner version fails the *build* rather than shipping broken caching.

Check the encoding yourself before trusting any patch recipe:

```sh
python3 -c 'd=open("/home/runner/bin/Runner.Worker.dll","rb").read()
print("utf16:", d.count("ACTIONS_RESULTS_URL".encode("utf-16-le")),
      "ascii:", d.count(b"ACTIONS_RESULTS_URL"))'
```

Related:
- ARC already sets `DisableUpdate=True` in its JIT config, so the runner won't self-update and
  undo the patch. Verify with `printenv ACTIONS_RUNNER_INPUT_JITCONFIG | base64 -d`.
- **`ACTIONS_CACHE_URL` is the legacy v1 endpoint and does nothing** with this cache server.
  Setting it is a no-op; only `ACTIONS_RESULTS_URL` matters, and it needs a trailing slash.
- The cache server implements the twirp `github.actions.results.api.v1.CacheService` routes,
  i.e. cache API **v2**, which needs **`actions/cache@v4.2+`**. Older action versions speak v1
  and will keep talking to GitHub no matter what you configure.

---

## 7. `subPathExpr` directories are never reclaimed — ~1 GB leaked per job

**Symptom.** The block device fills steadily. Two test pods left 1.2 GB behind.

**Cause.** kubelet **creates** `subPathExpr: $(POD_NAME)` directories but never deletes them.
Per finished job:

| Path | Size |
|---|---|
| `externals/<pod>` | ~595 MB |
| `work/<pod>` | ~325 MB–1.2 GB (checkout + restored caches) |
| `docker/<pod>` | ~13 MB + whatever the build pulled |
| `buildkit/<pod>` | build cache |

At ~1 GB/job a 250 GB device fills in a couple of hundred jobs.

**Fix.** `manifests/30-store-gc.yaml` — a CronJob every 15 min that deletes any store directory
not belonging to a live pod. It lists pods via the API and **bails out without deleting if that
call fails**, so a running job can never lose its workspace. Verified freeing 1.84 GB in one run.

**This is not optional.** Remove it and the device fills.

### …but a naive GC destroys running jobs (finding 7b)

The first version snapshotted the live pod list **once**, then deleted for as long as it
took. With 273 directories at ~1.3 GB each that took **4m40s**, and any pod starting inside
that window wasn't in the snapshot:

```
23:00:00Z  GC starts, snapshot = {9kxjg, jvjnv, pnmgb}
23:00:37Z  cohort 1 starts on phmmd   <- not in snapshot
23:00:42Z  cohort 2 starts on l5965   <- not in snapshot
23:03:32Z  both jobs die:
           Error: spawn .../_work/_tool/node/26.1.0/x64/bin/node ENOENT
23:04:40Z  GC completes
```

It deleted the `_work` of two running jobs, including the tool cache `actions/setup-node`
had just populated. The symptom looks like a corrupt tool cache or a flaky action; the cause
is the GC. Two jobs failing at the *same second* is the tell — that's an external event, not
two coincidental flakes.

Classic time-of-check-to-time-of-use. The fix has three guards, all of them load-bearing:

1. **Re-check liveness immediately before every delete** (`kubectl get pod <name>`), never
   from an up-front snapshot.
2. **Skip anything modified in the last 10 minutes**, so a pod that exists but isn't yet
   visible can't be caught.
3. **Abort if the API becomes unreachable mid-sweep** — "cannot tell" must never mean
   "safe to delete".

**The RBAC Role needs `get`, not just `list`.** `kubectl get pod <name>` requires the `get`
verb; with only `list` that call fails on RBAC, the script reads the failure as "pod is
gone", and it deletes a running job's directory anyway — reintroducing the very bug the
re-check exists to prevent. This was caught only because the first fix was tested with a
manual job before enabling the schedule.

**Watch the summary line.** It prints `freed …; kept N; skipped N`. If `kept` is **0** while
runners are live, guard 2 is broken — almost certainly the missing `get` permission.

---

## 8. NFS is wrong for CI workspaces — 16x slower on real cache restore

**Symptom.** `Cache pnpm store` takes 71 s. It looks like a slow cache download; it isn't.

Breakdown from a job log:

```
17:52:17  Cache hit
17:52:19  Cache Size: ~207 MB       <- 1.9s to look up + fetch
17:52:19  tar -xf ... unzstd
17:53:27  Cache restored            <- 68 SECONDS in extraction
```

**Cause.** The pnpm store is **35,883 files**. NFS is excellent sequentially and terrible on
per-file metadata round-trips.

Measured on this host, extracting the same real 207 MB cache blob:

| `_work` on | Extraction |
|---|---|
| NFS | **82 s** |
| iSCSI LUN | **5 s** |

Raw throughput is the opposite story — NFS 564 MB/s vs the local spinning disk's 69 MB/s —
which is exactly what makes this trap easy to fall into.

**Fix.** `_work` lives on the block device with everything else. In production the
`Cache pnpm store` step went **71 s → 7–13 s**.

**Beware the benchmark that misleads.** An earlier `git clone` test showed NFS only 10% slower
and was used to justify NFS — it moved 130 files. Benchmark with something file-count-realistic
or you will reach the wrong conclusion.

---

## 9. The service account token silently hijacks `kubectl`'s namespace

**Symptom.** A workflow's `kubectl port-forward` against its **own** kind cluster dies instantly:

```
kubectl port-forward svc/org-service 3001:3000 &
Error from server (NotFound): namespaces "arc-runners" not found
```

Tests then fail with `ECONNREFUSED` in a `BeforeAll` hook. It reads exactly like E2E flakiness.

**Cause.** client-go resolves a namespace as: `--namespace` → kubeconfig context namespace →
`"default"`. **But** if the context sets no namespace *and* in-cluster config is "possible" —
the SA token file exists **and** `KUBERNETES_SERVICE_HOST` is set, both of which kubelet gives
every pod — it uses the **service account's** namespace from
`/var/run/secrets/kubernetes.io/serviceaccount/namespace`. In a runner pod that is the runner
namespace.

This affects *any* `kubectl` invocation in *any* workflow that omits `-n`. A GitHub-hosted
runner has neither the token nor the env var, so it resolves to `default` and works.

**Fix.** `automountServiceAccountToken: false` on the runner pod. Safe because default
containerMode already assigns the chart's **no-permission** service account — the runner never
needed API access. **If you ever switch to `containerMode: kubernetes`, that mode does need
the token and this must be reverted.**

---

## 10. `fs.inotify.max_user_instances` — kind-in-CI dies at the default 128

**Symptom.**

```
failed to create fsnotify watcher: too many open files.
Consider adjusting inotify limits:
https://kind.sigs.k8s.io/docs/user/known-issues/#pod-errors-due-to-too-many-open-files
```

kind itself comes up fine. Downstream, an operator can't create watchers, never becomes
functional, and whatever waits on it times out — here `kubectl wait dopplersecret --all
--timeout=300s`. You also see `dial tcp 10.96.0.1:443: i/o timeout` as a secondary effect,
because kube-proxy and CNI use inotify too.

**Cause.** Ubuntu's default `fs.inotify.max_user_instances = 128`. It is a **per-UID** limit,
and everything kind runs inside dind is uid 0 on the host — so *all* concurrent runners share
one budget of 128. A single kind cluster with a full app stack exhausts it alone.
(`max_user_watches` was already 1048576 here and was not the problem.)

**Fix.** `manifests/50-node-tuning.yaml`, a DaemonSet with a privileged init container setting
`max_user_instances=8192`.

Why a DaemonSet: these sysctls are **not namespaced**, so a pod cannot set them for itself via
`securityContext.sysctls` (kubelet rejects non-namespaced sysctls). They must be set on the
host, and a one-off `sysctl -w` is lost on reboot. `host/99-kind-inotify.sysctl` is the
`/etc/sysctl.d/` equivalent if you have root and prefer that; then the DaemonSet is redundant.

---

## 11. `/dev/shm` is 64 MB — Chromium is unstable

**Symptom.** Occasional browser-test failures: `page.waitForSelector: Timeout 15000ms
exceeded` on an element that should render immediately, with no backend errors, no OOM kills,
no pod restarts, and a healthy cluster.

**Cause.** Kubernetes (like Docker) gives a container a 64 MB `/dev/shm`. Playwright documents
that Chromium is unstable at that size. GitHub-hosted runners are VMs where `/dev/shm` is
about half of RAM, so workflows assume gigabytes.

**Fix.** Mount a `medium: Memory` emptyDir at `/dev/shm`, 2Gi. Memory-backed emptyDirs count
against the container's memory limit, so keep it comfortably under.

**Honesty note:** this was diagnosed as the *likely* cause of 2/210 scenario failures, not
proven. It is a real divergence from hosted-runner behaviour producing exactly that class of
flakiness, and it is cheap to eliminate as a variable. If browser flakiness persists after
this, capture a Playwright trace before blaming infrastructure again.

---

## 12. Changing the pod spec rolls the runner set and briefly doubles requests

**Symptom.** CPU requests jumped from ~17% to **88%** during a routine `kubectl apply`.

**Cause.** When the `AutoscalingRunnerSet` pod template changes, ARC creates a **second**
`EphemeralRunnerSet` and drains the old one, so both exist briefly. In-flight jobs finish on
the old set — nothing is lost — but the node carries double the runner requests until it
settles (~90 s here).

**Practical advice.** Roll when CI is idle. It is safe mid-run, but it makes resource graphs
alarming and leaves less headroom. Changing only `minRunners`/`maxRunners` does **not** roll
pods — patch the `AutoscalingRunnerSet` directly for those.

---

## 14. Never set `GOROOT` (or any toolchain root) in the image

**Symptom.** `golangci-lint` fails across a whole Go package, in a job where `Setup Go`
reported success:

```
could not import go/token (/usr/local/go/src/go/token/position.go:8:2: could not import cmp
(-: # cmp
compile: version "go1.26.5" does not match go tool version "go1.26.3")) (typecheck)
```

**Cause.** This was **self-inflicted** — an earlier version of `runner-image/Dockerfile` added
`ENV GOROOT=/usr/local/go` alongside the Go install. `actions/setup-go` then installed Go
1.26.3 and prepended it to `PATH`, but it cannot override an explicitly-set `GOROOT`, so the
1.26.3 compiler read the image's 1.26.5 standard library. Every build in that job died.

Reproduced directly:

```
PATH go version  : go1.26.3        GOROOT still: /usr/local/go   -> compile: version mismatch
GOROOT unset     : go1.26.3        GOROOT: /tmp/sg/go            -> builds cleanly
```

**Fix.** Delete the `ENV GOROOT`. Go infers `GOROOT` from its own binary location (it resolves
the symlink), so `go env GOROOT` is correct in both cases — `/usr/local/go` when using the
image's Go, and setup-go's own directory when that's on `PATH`. GitHub's hosted images do not
set `GOROOT` globally either.

**Generalise this.** The same trap applies to `JAVA_HOME`, `GEM_HOME`, `PYTHONHOME`,
`NODE_PATH` and friends. Put the tool on `PATH`; do **not** pin its root in the environment,
or you break the matching `setup-*` action for everyone. `scripts/verify.sh` asserts `GOROOT`
is unset in a live runner pod.

**Wider lesson.** Adding a tool to close one gap (finding #4) opened a different one. After any
image change, re-run the *whole* verification, not just the thing you set out to fix.

## 13. Things that look like problems but aren't

- **`docker system prune` frees nothing.** On a box like this Docker reports 0 B reclaimable
  because every "dangling" image is still referenced by a running container. Disk pressure here
  came from **unrotated container logs** (a single container had a 17 GB `json-file` log) —
  check `/var/lib/docker/containers/*/*-json.log` before pruning anything.
- **NFS `rm -rf` throwing `No such file or directory`** on entries `readdir` just returned is
  normal NFS behaviour during bulk delete, not corruption.
- **The E2E job's own `kind-registry` on `127.0.0.1:5001`** does not collide with a host
  registry on the same port — it is published inside the pod's network namespace.
- **Cache-restore steps taking ~10 s** are normal once `_work` is on the block device. It was
  71 s on NFS.
