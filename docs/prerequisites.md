# Prerequisites

What must exist before `scripts/install.sh` will work.

## Host

Reference host this was built and verified on:

| | |
|---|---|
| OS | Ubuntu 22.04.5 LTS, kernel 5.15.0-186-generic |
| CPU / RAM | 72 cores / 252 GB (Dell PowerEdge R730) |
| Kubernetes | k3s v1.35.5+k3s1, single node, containerd 2.2.3 |
| Container runtime (host) | Docker 29.x **installed as a snap** |
| Local disk | single 931 GB spinning ST1000NX — slow (69 MB/s sequential) |
| Block storage | iSCSI LUN from a Synology NAS, ext4, `/mnt/arc-block` |
| Bulk storage | NFS from the same NAS (fast sequentially, bad on small files) |

Nothing here depends on the exact hardware, but three things matter:

1. **k8s ≥ 1.29** — native sidecars (`initContainers` with `restartPolicy: Always`) are used
   for dind and buildkitd.
2. **A real block device** for engine stores. Not NFS — see findings #1. If you only have NFS,
   stop and provision block storage first (iSCSI LUN, local SSD, cloud disk, anything ext4/xfs).
3. **Root access at least once**, to attach the block device. Everything else can be done with
   `kubectl` + `docker`.

### Docker as a snap

If Docker is a snap, its binaries are **not** on the default non-login `PATH`:

```sh
export PATH=$PATH:/snap/bin      # or call /snap/bin/docker
```

Data-root is `/var/snap/docker/common/var-lib-docker`.

### kubeconfig

k3s writes a world-readable kubeconfig, so no sudo is needed for `kubectl`:

```sh
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
```

### Tools

- `kubectl`, `docker`, `python3`, `curl`, `openssl` on the host.
- `jq` is **not** required (and was absent on the reference host) — the scripts use `python3`.
- `helm` is **not** required. Charts are installed via k3s's built-in `HelmChart` CRD so no
  native helm binary is added to the host. Confirm the CRD exists:
  ```sh
  kubectl get crd helmcharts.helm.cattle.io
  ```
  On a non-k3s cluster you will need real Helm and must translate `manifests/10-*`, `40-*`.

## Block device

`host/setup-iscsi.sh` attaches a Synology iSCSI LUN and mounts it at `/mnt/arc-block`.

**Before running it**, in DSM:
1. SAN Manager → LUN → Create. ~250 GB is comfortable.
2. SAN Manager → Target → Create, mapped to that LUN.
3. Permit this host's initiator IQN (the script prints it on first run).

Then:

```sh
sudo INSPECT=1 ./host/setup-iscsi.sh     # look, log out, change nothing
sudo FORMAT_OK=yes ./host/setup-iscsi.sh # for real
```

The script refuses to format anything carrying a filesystem or blkid signature, and refuses
to *mount* a pre-existing ext4 unless its label is `arc-block` — so it cannot wander into
unrelated data on a shared target. It writes a `_netdev,nofail` fstab entry so the mount
survives reboot without wedging boot if the NAS is down.

**If you are not using iSCSI**, provide `/mnt/arc-block` however you like. It must be a real
block-backed filesystem, and these four directories must exist inside the mount, mode 0777:

```
/mnt/arc-block/docker
/mnt/arc-block/buildkit
/mnt/arc-block/externals
/mnt/arc-block/work
```

Creating them **inside the mount** is deliberate: the pod spec uses `hostPath` with
`type: Directory`, so if the device is ever not mounted the paths don't exist and pods fail
loudly instead of silently filling the local disk.

## GitHub App

Runners register **org-level**. Create an App in the org:

Settings → Developer settings → GitHub Apps → New GitHub App

| Permission | Level |
|---|---|
| Repository → Actions | Read-only |
| Repository → Metadata | Read-only |
| Organization → **Self-hosted runners** | **Read and write** |

Uncheck Webhook "Active". Install it on the org, generate a private key, put the `.pem` on the
host (`chmod 600`), and record App ID + Installation ID in `config.env`.

> Editing an App's permissions later does **not** apply to existing installations — an org
> owner must accept the new permissions. `scripts/verify.sh` prints the *installation's*
> effective grant, which is what actually matters.

The Actions read permission is optional for running CI, but without it you cannot read job
logs from the host, which makes diagnosing failures much harder.

## Container registry

The runner image is custom and must be pullable by the cluster. On the reference host a plain
`registry:2` container serves `localhost:5001`, trusted via
`/etc/rancher/k3s/registries.yaml`:

```yaml
mirrors:
  "localhost:5001":
    endpoint:
      - "http://localhost:5001"
```

Any registry works; set `RUNNER_IMAGE_REPO` accordingly.
