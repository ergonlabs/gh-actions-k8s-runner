# Notes for agents working in this repo

Read this before touching anything. It is written for an AI agent asked to recreate, extend,
or debug this setup.

## What this repo is

A complete, reproducible definition of GitHub Actions self-hosted runners on k3s. It was
extracted from a working deployment after a long debugging session, and
**[docs/findings.md](docs/findings.md) is the most valuable file here** — it records 13
problems that each took real effort to diagnose, with the evidence that identified them.

## The single most important rule

**Before "simplifying" or "cleaning up" any manifest, search docs/findings.md for the thing
you are about to change.** Nearly every odd-looking choice is deliberate and load-bearing:

| Looks wrong | Actually required | Finding |
|---|---|---|
| `containerMode` isn't set to `dind` even though dind is used | the chart hardcodes its dind sidecar and drops user overrides | #2 |
| sidecars are `initContainers` with `restartPolicy: Always` | native sidecars, k8s ≥1.29 | #2 |
| a custom runner image instead of the official one | stock image lacks `libatomic1` and Go | #3, #4 |
| a binary patch to `Runner.Worker.dll` | the runner overwrites `ACTIONS_RESULTS_URL` | #6 |
| `--group=123` on buildkitd | socket is root-only otherwise; uid 1001 can't connect | #5 |
| `automountServiceAccountToken: false` | else `kubectl` in workflows targets the wrong namespace | #9 |
| a 2Gi memory-backed `/dev/shm` | k8s default 64 MB destabilises Chromium | #11 |
| a CronJob deleting directories | kubelet never reclaims `subPathExpr` dirs | #7 |
| workspaces on a block device, not the 6.7 TB NFS | NFS is 16x slower on real cache restore | #8 |
| helm pinned to 3.x when 4.x exists | parity with hosted `ubuntu-latest` | #4 |

## Working style that paid off here

These are not generic platitudes — each one caught a real bug in this project:

1. **Measure, don't assume.** A `git clone` benchmark said NFS was fine (130 files); the real
   workload was 35,883 files and 16x slower. Benchmark something representative.
2. **Verify the fix reached the thing you think it reached.** Setting `ACTIONS_RESULTS_URL`
   correctly on the pod did nothing, because the runner overwrote it. Always check the effect,
   not the configuration.
3. **Distrust recipes that can silently no-op.** Upstream documents a `sed` to patch the runner
   DLL; the string is UTF-16LE, so it matches nothing and yields a normal-looking broken image.
   Assert the change actually happened — the Dockerfile fails the build if it didn't.
4. **Read the chart, don't guess at its values.** Pulling and reading `_helpers.tpl` revealed
   that user-supplied `dind` containers are filtered out. No amount of values-file guessing
   would have worked.
5. **When something "looks like flakiness", check the environment differs from hosted runners.**
   Two E2E failures that looked like test flakiness were caused by an in-cluster service account
   token. Ask "what does a GitHub-hosted VM have that this pod doesn't?"
6. **Prefer failing loudly.** `hostPath: type: Directory` with store dirs created *inside* the
   mount means an unmounted device fails pods immediately rather than silently filling the
   local disk.

## Constraints of the reference host

- `sudo` is **password-gated** except a narrow allowlist. Anything needing root must be handed
  to the operator as a script plus instructions, not attempted unattended. `host/` holds those.
- The user is in the `docker` and `lxd` groups, which is already root-equivalent — so a
  privileged container is a legitimate way to read/modify root-owned paths, and mounting
  read-only (`:ro`) is good manners when only reading.
- Docker is a **snap**: binaries live in `/snap/bin`, not on the default non-login `PATH`.
- `jq` is **not installed**. Use `python3`.
- kubeconfig is world-readable at `/etc/rancher/k3s/k3s.yaml`; **no sudo needed for kubectl**.

## Changing the runner image

Two places reference the tag in `manifests/20-scale-set.yaml` — the `runner` container **and**
`init-dind-externals`. They must match; the init container copies `/home/runner/externals` out
of the same image.

Always use a date tag, never `:latest`, so running pods don't change underneath you.

After any rebuild, confirm the cache patch still applied (`scripts/install.sh` does this
automatically, and the Dockerfile asserts it at build time). If the assertion fires on a new
runner release, the DLL internals changed — re-check the patch rather than deleting it.

## Applying changes safely

Changing the pod template rolls the runner set: ARC starts a second `EphemeralRunnerSet` before
draining the old one, so requests briefly double (observed 17% → 88% CPU). In-flight jobs
finish; nothing is lost. Prefer rolling when CI is idle.

Changing only `minRunners`/`maxRunners` does **not** roll pods — patch the
`AutoscalingRunnerSet` directly:

```sh
kubectl patch autoscalingrunnerset <name> -n arc-runners --type=merge -p '{"spec":{"maxRunners":12}}'
```

(Then update `manifests/20-scale-set.yaml` too, or the next `apply` reverts it.)

## Diagnosing a CI failure

1. `kubectl logs -n arc-systems -l actions.github.com/scale-set-name=<name>` — did the job
   even dispatch?
2. `kubectl get events -n arc-runners --sort-by=.lastTimestamp` — pod-level problems.
3. Job logs from GitHub. With `actions: read` on the App you can fetch them from the host; see
   `docs/runbook.md` for the JWT → installation-token → logs recipe.
4. Ask whether the failure is something a hosted runner wouldn't hit (missing tool, in-cluster
   env var, container resource limit). That framing found several bugs here.

Do not conclude "flaky test" until the environment has been ruled out — and say so honestly
when the evidence is circumstantial rather than proven. Finding #11 is explicitly labelled
*suspected, not proven*, and that labelling matters.
