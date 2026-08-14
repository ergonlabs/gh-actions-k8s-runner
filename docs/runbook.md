# Runbook

Day-2 operations. `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml` first (no sudo needed).

## Health at a glance

```sh
./scripts/verify.sh                                  # full check, ~2 min (spawns a pod)

kubectl get pods -n arc-systems                      # controller + listener
kubectl get autoscalingrunnerset -n arc-runners      # min/max/current
kubectl get pods -n arc-runners -w                   # jobs appearing/vanishing
df -h /mnt/arc-block                                 # storage headroom
```

Listener activity (did a job dispatch?):

```sh
kubectl logs -n arc-systems -l actions.github.com/scale-set-name=ergonlabs-k8s --tail=30
```

## Scaling

`minRunners`/`maxRunners` changes do **not** roll pods:

```sh
kubectl patch autoscalingrunnerset ergonlabs-k8s -n arc-runners --type=merge \
  -p '{"spec":{"maxRunners":16}}'
```

Update `manifests/20-scale-set.yaml` to match, or the next `apply` reverts it.

Budget ~3 CPU / 6Gi of *requests* per concurrent job. Check headroom with:

```sh
kubectl describe node <node> | sed -n '/Allocated resources/,/Events/p'
```

## Rebuilding the runner image

```sh
cd runner-image
docker build -t localhost:5001/ergonlabs/actions-runner:$(date +%F) .
docker push  localhost:5001/ergonlabs/actions-runner:$(date +%F)
```

Then bump the tag in **two places** in `manifests/20-scale-set.yaml` (the `runner` container
and `init-dind-externals` — they must match) and `RUNNER_IMAGE_TAG` in `config.env`, then
`kubectl apply -f manifests/20-scale-set.yaml`.

Always a date tag, never `:latest`, so running pods don't change underneath you.

If the build fails on the cache-patch assertion, a new runner release changed
`Runner.Worker.dll`. **Do not delete the assertion** — re-check the patch (findings #6).

### Keeping up with hosted-runner tooling

Workflows assume whatever `ubuntu-latest` preinstalls. When bumping versions, match
`actions/runner-images/images/ubuntu/Ubuntu2404-Readme.md`:

```sh
curl -s https://raw.githubusercontent.com/actions/runner-images/main/images/ubuntu/Ubuntu2404-Readme.md \
  | grep -iE '^- (Helm|Kubectl|Kustomize|Kind|yq) |^#### Go' -A4
```

Keep helm on **3.x** unless you have checked every chart against Helm 4's breaking changes.

## Upgrading ARC

Controller and scale-set charts **must be the same version**; upgrading one alone breaks the
listener. Bump `ARC_CHART_VERSION` in `config.env` and the `version:` in both
`manifests/10-controller.yaml` and `manifests/20-scale-set.yaml`, then apply the controller
first, then the scale set.

Latest chart version:

```sh
TOK=$(curl -s "https://ghcr.io/token?scope=repository:actions/actions-runner-controller-charts/gha-runner-scale-set-controller:pull&service=ghcr.io" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $TOK" \
  https://ghcr.io/v2/actions/actions-runner-controller-charts/gha-runner-scale-set-controller/tags/list \
  | python3 -c 'import sys,json,re;t=[x for x in json.load(sys.stdin)["tags"] if re.match(r"^\d+\.\d+\.\d+$",x)];t.sort(key=lambda v:tuple(map(int,v.split("."))));print(t[-5:])'
```

## Reading job logs from the host

Needs `actions: read` on the App installation. Mint an installation token:

```sh
APP_ID=...; INSTALL_ID=...; PEM=/path/to/key.pem
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
NOW=$(date +%s)
H=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
P=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((NOW-60)) $((NOW+540)) "$APP_ID" | b64url)
S=$(printf '%s.%s' "$H" "$P" | openssl dgst -sha256 -sign "$PEM" -binary | b64url)
TOK=$(curl -s -X POST -H "Authorization: Bearer $H.$P.$S" \
  "https://api.github.com/app/installations/$INSTALL_ID/access_tokens" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# jobs for a run (NOTE: paginate — per_page caps at 100 and big matrices exceed it)
curl -s -H "Authorization: token $TOK" \
  "https://api.github.com/repos/<org>/<repo>/actions/runs/<run_id>/jobs?per_page=100&page=1"

# a job's full log
curl -sL -H "Authorization: token $TOK" \
  "https://api.github.com/repos/<org>/<repo>/actions/jobs/<job_id>/logs" -o job.log
```

Installation tokens expire after an hour; just mint another.

## Storage

The GC runs every 15 min. Check it:

```sh
kubectl get cronjob arc-store-gc -n arc-runners
kubectl logs -n arc-runners job/$(kubectl get jobs -n arc-runners --no-headers \
  | grep store-gc | tail -1 | awk '{print $1}')
```

Force a run:

```sh
kubectl create job -n arc-runners gc-now --from=cronjob/arc-store-gc
```

Inspect the device (dirs are root-owned; use a container):

```sh
docker run --rm -v /mnt/arc-block:/b:ro alpine du -sh /b/*
```

If the device fills, the GC is not running. Do not delete store dirs of **live** pods.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Listener not Running, 403s | App installation lacks `organization_self_hosted_runners: write`. Editing App permissions does **not** apply to existing installs — an org owner must accept |
| Pods stuck `ContainerCreating`, hostPath errors | block device not mounted. `mountpoint /mnt/arc-block` |
| Builds fail `invalid argument` on an overlay mount | an engine store is pointing at NFS (findings #1) |
| `buildx` → `context deadline exceeded`, builder `inactive` | buildkitd socket is root-only; `--group=123` missing (#5) |
| Caches still going to GitHub | action older than v4.2, or the DLL patch didn't apply (#6) |
| `kubectl` in a workflow hits the wrong namespace | SA token mounted; `automountServiceAccountToken: false` (#9) |
| kind fails `too many open files` | `fs.inotify.max_user_instances` too low (#10) |
| Flaky browser tests, selector timeouts | `/dev/shm` too small (#11) |
| Cache restore takes >60 s | `_work` is on NFS (#8) |
| Node job dies `libatomic.so.1` | stock runner image in use instead of the custom one (#3) |
| `go: not found` | Go missing from the image (#4) |
| CPU requests suddenly ~doubled | a pod-spec change is rolling the runner set; it settles (#12) |

## Host disk

Disk pressure on this class of host is usually **unrotated container logs**, not images —
`docker system prune` often reports 0 B reclaimable because "dangling" images are still
referenced by running containers.

```sh
docker run --rm -v /var/snap/docker/common/var-lib-docker/containers:/c:ro alpine \
  sh -c 'du -m /c/*/*-json.log | sort -rn | head'
```

Truncate (don't `rm` — the daemon holds the fd):

```sh
sudo sh -c ': > /var/lib/docker/containers/<id>/<id>-json.log'
```

The durable fix is log rotation in `daemon.json` (`max-size`/`max-file`), which requires a
daemon restart and only applies to containers created afterwards.

`host/prune-disk.sudoers` grants passwordless `k3s crictl rmi --prune` if you want image
pruning unattended — note that on a box like this it typically frees very little.
