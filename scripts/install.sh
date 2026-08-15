#!/usr/bin/env bash
# install.sh — stand up the whole ARC setup, in order.
#
#   cp config.env.example config.env && $EDITOR config.env
#   ./scripts/install.sh
#
# Safe to re-run: every step is apply/create-or-update. It will NOT touch the block
# device (that's host/setup-iscsi.sh, which needs root and is run once by hand).
#
# Read docs/findings.md before changing any manifest. Most of the odd-looking choices
# in them are load-bearing.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD

[ -f config.env ] || { echo "ERROR: config.env not found. cp config.env.example config.env"; exit 1; }
# shellcheck disable=SC1091
. ./config.env

export KUBECONFIG
DOCKER="${DOCKER_BIN:-docker}"
K() { kubectl "$@"; }

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
step "Preflight"

command -v kubectl >/dev/null || die "kubectl not found"
[ -x "$DOCKER" ] || command -v "$DOCKER" >/dev/null || die "docker not found at $DOCKER (it may be a snap: /snap/bin/docker)"
K version >/dev/null 2>&1 || die "cannot reach the cluster with KUBECONFIG=$KUBECONFIG"

K get crd helmcharts.helm.cattle.io >/dev/null 2>&1 \
  || die "helmcharts.helm.cattle.io CRD not found.
       This install uses k3s's built-in HelmChart CRD so no native helm binary is needed.
       On a non-k3s cluster, install Helm and translate manifests/10-* and 40-* by hand."

# k8s >= 1.29 for native sidecars (dind/buildkitd use restartPolicy: Always in initContainers)
MINOR=$(K version -o json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["serverVersion"]["minor"].strip("+"))')
[ "${MINOR:-0}" -ge 29 ] || die "k8s minor $MINOR < 29; native sidecars are required (see docs/findings.md #2)"

[ -f "$GITHUB_APP_PRIVATE_KEY" ] || die "App private key not found at $GITHUB_APP_PRIVATE_KEY"
openssl rsa -in "$GITHUB_APP_PRIVATE_KEY" -noout -check >/dev/null 2>&1 \
  || die "$GITHUB_APP_PRIVATE_KEY is not a valid RSA private key"
PERM=$(stat -c '%a' "$GITHUB_APP_PRIVATE_KEY")
[ "$PERM" = "600" ] || echo "  WARNING: $GITHUB_APP_PRIVATE_KEY is mode $PERM; chmod 600 recommended"

mountpoint -q "$ARC_BLOCK_MOUNT" \
  || die "$ARC_BLOCK_MOUNT is not a mountpoint.
       Engine stores MUST be on a block device — overlayfs cannot use an NFS upperdir.
       See docs/findings.md #1, then run: sudo ./host/setup-iscsi.sh"

for d in docker buildkit externals work dind-logs; do
  [ -d "$ARC_BLOCK_MOUNT/$d" ] \
    || die "$ARC_BLOCK_MOUNT/$d missing. Create the store dirs INSIDE the mount (mode 0777).
       They live inside the mount on purpose: hostPath type: Directory then fails loudly if
       the device is not mounted, instead of silently filling the local disk."
done
echo "  block device OK: $(df -h "$ARC_BLOCK_MOUNT" | awk 'NR==2{print $2" total, "$4" free"}')"
echo "  preflight passed"

# ---------------------------------------------------------------- runner image
step "Runner image"
IMAGE="$RUNNER_IMAGE_REPO:$RUNNER_IMAGE_TAG"
if "$DOCKER" manifest inspect "$IMAGE" >/dev/null 2>&1 || "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "  $IMAGE already present; skipping build (delete it to force a rebuild)"
else
  echo "  building $IMAGE"
  "$DOCKER" build -t "$IMAGE" "$ROOT/runner-image"
  echo "  pushing"
  "$DOCKER" push "$IMAGE"
fi

# The Dockerfile asserts the Runner.Worker.dll patch applied; re-check it in the built image
# because a silently-unpatched image means actions/cache quietly uses GitHub (findings #6).
"$DOCKER" run --rm --entrypoint python3 "$IMAGE" -c '
d=open("/home/runner/bin/Runner.Worker.dll","rb").read()
o=d.count("ACTIONS_RESULTS_URL".encode("utf-16-le")); p=d.count("ACTIONS_RESULTS_ORL".encode("utf-16-le"))
assert o==0 and p==1, f"cache patch NOT applied (orig={o} patched={p})"
print("  cache patch verified")'

# ---------------------------------------------------------------- namespaces + secret
step "Namespaces"
K apply -f manifests/00-namespaces.yaml

step "GitHub App secret"
K create secret generic arc-github-app -n arc-runners \
  --from-literal=github_app_id="$GITHUB_APP_ID" \
  --from-literal=github_app_installation_id="$GITHUB_APP_INSTALLATION_ID" \
  --from-file=github_app_private_key="$GITHUB_APP_PRIVATE_KEY" \
  --dry-run=client -o yaml | K apply -f -
echo "  secret arc-github-app created/updated"

# ---------------------------------------------------------------- controller
step "ARC controller (chart $ARC_CHART_VERSION)"
K apply -f manifests/10-controller.yaml
echo "  waiting for controller..."
K wait --for=condition=Available deploy -n arc-systems --all --timeout=300s 2>/dev/null || true
K get pods -n arc-systems

# ---------------------------------------------------------------- node tuning
step "Node tuning (inotify limits for kind-in-CI)"
K apply -f manifests/50-node-tuning.yaml
K rollout status ds/arc-node-tuning -n arc-systems --timeout=180s || true
echo "  fs.inotify.max_user_instances = $(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo '?')"

# ---------------------------------------------------------------- cache server
step "Self-hosted actions/cache server"
if [ ! -d "$ARC_BLOCK_MOUNT/cache" ]; then
  echo "  NOTE: $ARC_BLOCK_MOUNT/cache missing — create it owned by uid 1000, mode 0775:"
  echo "      $DOCKER run --rm -v $ARC_BLOCK_MOUNT:/b alpine sh -c 'mkdir -p /b/cache && chown 1000:1000 /b/cache && chmod 775 /b/cache'"
fi
K apply -f manifests/40-cache-server.yaml

# ---------------------------------------------------------------- hub mirror
step "Docker Hub pull-through cache"
if [ ! -d "$ARC_BLOCK_MOUNT/hub-mirror" ]; then
  echo "  NOTE: $ARC_BLOCK_MOUNT/hub-mirror missing — the mirror pod will not start until it exists:"
  echo "      $DOCKER run --rm -v $ARC_BLOCK_MOUNT:/b alpine sh -c 'mkdir -p /b/hub-mirror'"
fi
K apply -f manifests/45-hub-mirror.yaml

# ---------------------------------------------------------------- scale set
step "Runner scale set ($RUNNER_SCALE_SET_NAME)"
K apply -f manifests/20-scale-set.yaml

step "Per-pod store GC (NOT optional — see docs/findings.md #7)"
K apply -f manifests/30-store-gc.yaml
K apply -f manifests/35-store-gc-pressure.yaml

# ---------------------------------------------------------------- done
step "Done"
cat <<EOF
Verify with:   ./scripts/verify.sh

Workflows target these runners with:

    jobs:
      build:
        runs-on: $RUNNER_SCALE_SET_NAME

The listener may take a minute to register. Watch it:

    kubectl get autoscalingrunnerset -n arc-runners
    kubectl logs -n arc-systems -l actions.github.com/scale-set-name=$RUNNER_SCALE_SET_NAME --tail=20
EOF
