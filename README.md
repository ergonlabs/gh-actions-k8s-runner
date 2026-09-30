# arc-infra

GitHub Actions self-hosted runners on k3s, using [Actions Runner
Controller](https://github.com/actions/actions-runner-controller). Two pools since
2026-08-20:

- **`ergonlabs-k8s`** (large) — a fresh pod per job with Docker (dind), BuildKit, and a
  Kubernetes toolchain matching GitHub's hosted `ubuntu-latest` image. For jobs that build
  Docker images or run kind.
- **`ergonlabs-k8s-small`** — runner container only, no Docker at all. For jobs that never
  touch Docker (lint/typecheck/test/build) — lighter pods, higher `maxRunners`.

Workflows opt in with:

```yaml
jobs:
  build-docker-image:            # or anything needing kind/dind/buildx
    runs-on: ergonlabs-k8s
  lint-and-test:                 # anything that never touches Docker
    runs-on: ergonlabs-k8s-small
```

Everything needed to recreate this from scratch is in this repo. **No secrets are committed** —
the GitHub App private key stays on the host.

## Quick start

```sh
cp config.env.example config.env && $EDITOR config.env

sudo ./host/setup-iscsi.sh      # one-time: attach the block device (needs root)
./scripts/install.sh            # everything else
./scripts/verify.sh             # 45+ checks incl. in-pod invariants for both pools
```

See [docs/prerequisites.md](docs/prerequisites.md) for what must exist first — a block
device, a GitHub App, and a registry.

## What gets deployed

| Component | Where | Purpose |
|---|---|---|
| ARC controller | `arc-systems` | reconciles runner scale sets |
| `ergonlabs-k8s` listener | `arc-systems` | large pool: long-polls GitHub for jobs, scales 0→3 |
| `ergonlabs-k8s-small` listener | `arc-systems` | small pool: long-polls GitHub for jobs, scales 0→30 |
| large-pool runner pods | `arc-runners` | ephemeral, one per job; runner + dind + buildkitd |
| small-pool runner pods | `arc-runners` | ephemeral, one per job; runner only, no Docker |
| `arc-cache` | `arc-runners` | self-hosted `actions/cache` server, shared by both pools |
| `arc-hub-mirror` | `arc-runners` | authenticated Docker Hub pull-through cache |
| `arc-store-gc` | `arc-runners` | CronJob reclaiming per-pod dirs (**not optional**) |
| `arc-store-gc-pressure` | `arc-runners` | reactive companion to arc-store-gc, triggers on disk pressure |
| `arc-node-tuning` | `arc-systems` | DaemonSet raising host inotify + AIO limits |
| `arc-resource-sampler` | `arc-runners` | DaemonSet sampling every runner container's CPU/memory each second (per-job reports) |

Charts are installed through k3s's built-in `HelmChart` CRD, so **no native `helm` binary is
added to the host**.

ARC needs **no ports, no Ingress, no NodePort** — it long-polls outbound only.

## Layout

```
manifests/       numbered, apply in order
  00-namespaces  10-controller  20-scale-set  21-scale-set-small  30-store-gc
  35-store-gc-pressure  40-cache-server  45-hub-mirror  47-resource-sampler  50-node-tuning
runner-image/    Dockerfile for the custom runner image (+ resource-report/ hook + renderer)
resource-report/ sampler.py (the DaemonSet's script) and action.yml (artifact upload action)
host/            things that need root: iSCSI setup, sysctl drop-in, sudoers drop-in
scripts/         install.sh, verify.sh
docs/            architecture, findings, prerequisites, runbook
config.env       your values (gitignored; copy from config.env.example)
```

## Per-job resource reports

Every job on either pool gets a CPU/memory report, with no workflow changes:

- `arc-resource-sampler` (`manifests/47-resource-sampler.yaml`) reads each runner pod's
  cgroup v2 counters from the host once per second, **per container** (runner, dind,
  buildkitd, …), into `/mnt/arc-block/resource-metrics/<pod>/`. Finished pods are gzipped into
  `_archive/<day>/` for 30 days — raw material for pool-level sizing analysis.
- Each runner mounts its own pod's slice read-only at `/resource-metrics`, and the runner's
  job-completed hook renders it into the **job summary**: peak/p95 working set vs limit, OOM
  kills, CPU avg/peak vs limit, throttling, and the heaviest steps.
- For the full single-page HTML report (charts per container, memory composition, pressure
  stalls, node context, step bands) as a **build artifact**, add one step at the end of a job:

  ```yaml
  - uses: ergonlabs/gh-actions-k8s-runner/resource-report@main
    if: always()
  ```

  Job hooks can't upload artifacts (the runner withholds `ACTIONS_RUNTIME_TOKEN` from
  scripts), which is why this one step is needed. On runners without the sampler it logs a
  notice and does nothing.

Memory means **working set** (`memory.current − inactive_file`), what kubelet and the OOM
killer act on. This kernel (5.15) has no `memory.peak`, so peaks exist only because sampling
catches them.

## Read this before changing anything

**[docs/findings.md](docs/findings.md)** documents 14 non-obvious problems hit while building
this, with evidence. Most of the strange-looking choices in the manifests are load-bearing.
The short version:

- **Engine stores must be on a block device.** overlayfs cannot use an NFS upperdir — dockerd
  starts fine and then every build fails with `invalid argument`.
- **`containerMode: dind` is deliberately NOT used.** The chart hardcodes its dind sidecar and
  filters out any user-supplied container named `dind`, so `/var/lib/docker` can't be moved.
  The sidecars are hand-rolled instead.
- **The runner image is custom and must stay custom.** The stock image lacks `libatomic1`
  (breaks all Node jobs) and Go (breaks cross-workspace typechecks), and needs a patched
  `Runner.Worker.dll` for the cache server to work at all.
- **Job workspaces belong on the block device, not NFS.** Restoring a real 207 MB / 35,883-file
  pnpm cache: 82 s on NFS vs 5 s on the LUN.
- **`automountServiceAccountToken: false` is required.** Otherwise `kubectl` inside a workflow
  silently targets the *runner's* namespace instead of `default`, which breaks any workflow
  managing its own cluster and looks exactly like E2E flakiness.
- **The store-GC CronJob is not optional.** kubelet never reclaims `subPathExpr` dirs; ~1 GB
  leaks per job.

## Day-2

[docs/runbook.md](docs/runbook.md) — upgrades, image rebuilds, scaling, troubleshooting, and
the failure signatures worth recognising.
