# Architecture

## Shape

```
GitHub (org: ergonlabs)
   ▲ outbound long-poll only — no inbound, no ingress, no ports
   │
┌──┴─────────────────────────── k3s node ──────────────────────────────┐
│                                                                       │
│  arc-systems                    arc-runners                           │
│  ┌──────────────────┐           ┌──────────────────────────────────┐  │
│  │ ARC controller   │──creates─▶│ runner pod (one per job)         │  │
│  │ scale-set        │           │  ┌────────┬──────┬────────────┐  │  │
│  │   listener       │           │  │ runner │ dind │ buildkitd  │  │  │
│  └──────────────────┘           │  └────────┴──────┴────────────┘  │  │
│  ┌──────────────────┐           └──────────────────────────────────┘  │
│  │ node-tuning DS   │           ┌──────────────┐  ┌────────────────┐  │
│  │ (inotify sysctl) │           │ arc-cache    │  │ arc-store-gc   │  │
│  └──────────────────┘           │ (cache srv)  │  │ (CronJob /15m) │  │
│                                 └──────────────┘  └────────────────┘  │
│                                                                       │
│  /mnt/arc-block  (iSCSI LUN, ext4)   ← ALL runner state lives here     │
│     docker/  buildkit/  externals/  work/  cache/                     │
└───────────────────────────────────────────────────────────────────────┘
```

## Two pools

The diagram and "Runner pod" section below describe the **large pool** (`ergonlabs-k8s`,
`manifests/20-scale-set.yaml`) — full dind + buildkitd, for jobs that build Docker images or
run kind. Since 2026-08-20 there's also a **small pool** (`ergonlabs-k8s-small`,
`manifests/21-scale-set-small.yaml`): a single `runner` container, no Docker at all, for jobs
that never touch it (lint/typecheck/test/build). Both pools share the same custom runner image,
`arc-cache`, `arc-hub-mirror`, and `arc-store-gc`; the small pool's runner pods just don't mount
the dind/buildkit volumes or set the Docker-related env vars, so there's nothing extra to
diagram for it. See `21-scale-set-small.yaml`'s own comments for the full rationale.

## Runner pod (large pool)

Four containers, all from `manifests/20-scale-set.yaml`:

| Container | Kind | Role |
|---|---|---|
| `init-dind-externals` | init | copies `/home/runner/externals` out of the runner image |
| `dind` | native sidecar | `dockerd`, privileged, `--data-root=/var/lib/docker` |
| `buildkitd` | native sidecar | BuildKit, privileged, `--group=123` |
| `runner` | main | the Actions runner itself, uid 1001 |

`dind` and `buildkitd` are `initContainers` with `restartPolicy: Always` — native sidecars,
which need k8s ≥ 1.29. They start before the runner and keep running alongside it.

Both engines are present in every pod so workflows can migrate from `docker build` to
`buildx` gradually without changing `runs-on`.

### Why the sidecars are hand-rolled

The chart's `containerMode: dind` hardcodes its dind sidecar and explicitly filters out any
user-supplied container named `dind`, so `/var/lib/docker` cannot be relocated through values.
With `containerMode` unset, the chart passes `initContainers`, `containers` and `volumes`
through verbatim. See findings #2.

## Storage

Everything is on the block device. Nothing that matters is on NFS.

| Mount | Backed by | Why |
|---|---|---|
| `/var/lib/docker` | `/mnt/arc-block/docker/<pod>` | overlayfs needs a block device |
| `/var/lib/buildkit` | `/mnt/arc-block/buildkit/<pod>` | same |
| `/home/runner/externals` | `/mnt/arc-block/externals/<pod>` | ~595 MB copied per pod |
| `/home/runner/_work` | `/mnt/arc-block/work/<pod>` | CI workspaces are small-file heavy |
| cache server data | `/mnt/arc-block/cache` | plus its sqlite db (needs real locking) |
| `/dev/shm` | memory-backed emptyDir, 2Gi | Chromium stability |
| dind/buildkit sockets | emptyDir | sockets only, no payload |

Per-pod isolation is `subPathExpr: $(POD_NAME)`, which requires a `POD_NAME` `fieldRef` in
every container mounting a shared volume. kubelet creates those subdirectories and **never
deletes them**, hence the GC CronJob.

`hostPath` uses `type: Directory` and the store dirs live *inside* the mount, so an unmounted
device fails pods loudly instead of silently filling the local disk.

## Caching

`actions/cache` is redirected to an in-cluster server (`arc-cache:3000`) via
`ACTIONS_RESULTS_URL`. Cache blobs and its sqlite DB sit on the block device.

Three things are required for this to work at all, and each fails silently:

1. `actions/cache@v4.2+` in the workflow — the server implements only cache API **v2** (twirp).
2. `ACTIONS_RESULTS_URL` with a **trailing slash**. `ACTIONS_CACHE_URL` is the legacy v1
   endpoint and does nothing.
3. The **patched `Runner.Worker.dll`**, because the runner otherwise overwrites the variable
   from the job message.

Auth needs no shared secret: the server verifies each runner's `ACTIONS_RUNTIME_TOKEN` against
GitHub's OIDC JWKS, so a runner can only reach its own repo's cache scope. Never set
`SKIP_TOKEN_VALIDATION` — upstream is explicit that it disables signature verification and
lets any client poison any repo's cache.

## The runner image

Built from `runner-image/Dockerfile`, based on `ghcr.io/actions/actions-runner`.

There is no drop-in alternative: GitHub's real `ubuntu-latest` comes from
`actions/runner-images` and is a ~30 GB **VM** image, not a container. Extending the official
ARC image is the supported path.

It adds:

- `libatomic1` — without it every Node job dies (findings #3)
- Go, helm, kubectl, kustomize, kind, yq — versions **matched to hosted `ubuntu-24.04`**, so a
  workflow behaves the same here as on `ubuntu-latest` (note helm 3.x, not 4.x)
- `build-essential`, `python3`, `zstd`, `jq`, `git-lfs`, `unzip`/`zip`, `openssh-client`
- the `Runner.Worker.dll` cache patch

Deliberately excluded, though `ubuntu-latest` has them: `minikube`, `podman`, `buildah`,
`skopeo` — large and redundant given dind and buildkitd are already in every pod.

## Sizing

Both pools have real CPU **limits** now (added 2026-08-20 — before that only memory was
capped), not just requests, so a runaway job in either pool throttles instead of starving its
neighbors.

**Large pool** (`ergonlabs-k8s`): each job requests ~3 CPU / 6Gi (runner 2/4Gi + dind 0.5/1Gi +
buildkit 0.5/1Gi), limits ~8 CPU / 28Gi. `maxRunners: 3` ⇒ worst-case ceiling ~24 CPU / ~84Gi.

**Small pool** (`ergonlabs-k8s-small`): each job requests 250m CPU / 768Mi, limits 1 CPU / 3Gi.
`maxRunners: 30` ⇒ worst-case ceiling ~30 CPU / ~90Gi.

Combined worst-case ceiling (~54 CPU / ~174Gi) leaves real headroom on the reference 72-core /
252Gi node for the ~30 native Docker containers (measured ~41Gi actual usage 2026-08-20) plus
k3s/containerd overhead — and that ceiling only bites in the pathological all-pods-bursting-
simultaneously case, not steady state, since requests for both pools combined at full scale-out
are only ~16.5 CPU. `minRunners: 0` on both, so idle CI costs nothing.

Storage per job is roughly 1 GB in the large pool (595 MB externals + checkout + restored
caches), much less in the small pool (no dind/buildkit stores at all — just checkout + node
caches). `arc-store-gc` sweeps every 5 minutes, backed by `arc-store-gc-pressure` (a reactive
watcher added 2026-08-20 that triggers an out-of-cycle sweep the moment disk usage crosses 90%,
for bursts faster than any fixed interval can react to).

## Security posture

- dind and buildkitd are **privileged**. On a single-node box with no untrusted tenants that's
  a reasonable trade, but a workflow from a fork would effectively run as root on the host.
  Keep the App installed only on repos you trust.
- Runner pods get the chart's **no-permission** service account and no token is mounted.
- The GitHub App holds `organization_self_hosted_runners: write` plus read-only Actions and
  Metadata — no repo write access anywhere.
