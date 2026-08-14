# arc-infra

GitHub Actions self-hosted runners on k3s, using [Actions Runner
Controller](https://github.com/actions/actions-runner-controller). Each job gets a fresh pod
with Docker (dind), BuildKit, and a Kubernetes toolchain matching GitHub's hosted
`ubuntu-latest` image.

Workflows opt in with:

```yaml
jobs:
  build:
    runs-on: ergonlabs-k8s
```

Everything needed to recreate this from scratch is in this repo. **No secrets are committed** —
the GitHub App private key stays on the host.

## Quick start

```sh
cp config.env.example config.env && $EDITOR config.env

sudo ./host/setup-iscsi.sh      # one-time: attach the block device (needs root)
./scripts/install.sh            # everything else
./scripts/verify.sh             # 25+ checks incl. in-pod invariants
```

See [docs/prerequisites.md](docs/prerequisites.md) for what must exist first — a block
device, a GitHub App, and a registry.

## What gets deployed

| Component | Where | Purpose |
|---|---|---|
| ARC controller | `arc-systems` | reconciles runner scale sets |
| `ergonlabs-k8s` listener | `arc-systems` | long-polls GitHub for jobs, scales 0→N |
| runner pods | `arc-runners` | ephemeral, one per job; runner + dind + buildkitd |
| `arc-cache` | `arc-runners` | self-hosted `actions/cache` server |
| `arc-store-gc` | `arc-runners` | CronJob reclaiming per-pod dirs (**not optional**) |
| `arc-node-tuning` | `arc-systems` | DaemonSet raising host inotify limits |

Charts are installed through k3s's built-in `HelmChart` CRD, so **no native `helm` binary is
added to the host**.

ARC needs **no ports, no Ingress, no NodePort** — it long-polls outbound only.

## Layout

```
manifests/       numbered, apply in order
  00-namespaces  10-controller  20-scale-set  30-store-gc  40-cache-server  50-node-tuning
runner-image/    Dockerfile for the custom runner image
host/            things that need root: iSCSI setup, sysctl drop-in, sudoers drop-in
scripts/         install.sh, verify.sh
docs/            architecture, findings, prerequisites, runbook
config.env       your values (gitignored; copy from config.env.example)
```

## Read this before changing anything

**[docs/findings.md](docs/findings.md)** documents 13 non-obvious problems hit while building
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
