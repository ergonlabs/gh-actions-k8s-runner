#!/usr/bin/env bash
# verify.sh — check every part of the setup, including the non-obvious ones.
#
#   ./scripts/verify.sh
#
# Read-only. Spawns one runner pod (minRunners=1) to check in-pod invariants, then
# scales back to whatever it was. Exits non-zero if any check fails.
set -uo pipefail

cd "$(dirname "$0")/.."
[ -f config.env ] || { echo "ERROR: config.env not found"; exit 1; }
# shellcheck disable=SC1091
. ./config.env
export KUBECONFIG
DOCKER="${DOCKER_BIN:-docker}"

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# ---------------------------------------------------------------- cluster
step "Cluster components"
kubectl get deploy -n arc-systems 2>/dev/null | grep -q gha-rs-controller \
  && ok "ARC controller present" || bad "ARC controller missing"
[ "$(kubectl get pods -n arc-systems -l app.kubernetes.io/name=gha-rs-controller \
      -o jsonpath='{.items[0].status.phase}' 2>/dev/null)" = "Running" ] \
  && ok "controller Running" || bad "controller not Running"
kubectl get autoscalingrunnerset "$RUNNER_SCALE_SET_NAME" -n arc-runners >/dev/null 2>&1 \
  && ok "scale set $RUNNER_SCALE_SET_NAME exists" || bad "scale set missing"
kubectl get pods -n arc-systems 2>/dev/null | grep -q "$RUNNER_SCALE_SET_NAME.*listener.*Running" \
  && ok "listener Running (authenticated to GitHub)" || bad "listener not Running — check App permissions"
kubectl get cronjob arc-store-gc -n arc-runners >/dev/null 2>&1 \
  && ok "store GC CronJob present" || bad "store GC missing — the block device WILL fill"
kubectl get ds arc-node-tuning -n arc-systems >/dev/null 2>&1 \
  && ok "node-tuning DaemonSet present" || bad "node-tuning missing — kind-in-CI will fail"
kubectl get svc arc-cache -n arc-runners >/dev/null 2>&1 \
  && ok "cache server service present" || bad "cache server missing"
kubectl get svc arc-hub-mirror -n arc-runners >/dev/null 2>&1 \
  && ok "hub mirror service present" || bad "hub mirror missing — Docker Hub 429s will return"
kubectl get deploy arc-store-gc-pressure -n arc-runners -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -q '^1$' \
  && ok "store-gc pressure watcher running" || bad "pressure watcher not ready — ENOSPC bursts won't get a fast reaction"

# ---------------------------------------------------------------- host
step "Host"
mountpoint -q "$ARC_BLOCK_MOUNT" \
  && ok "$ARC_BLOCK_MOUNT mounted ($(df -h "$ARC_BLOCK_MOUNT" | awk 'NR==2{print $4" free"}'))" \
  || bad "$ARC_BLOCK_MOUNT NOT mounted — pods will fail (by design)"
for d in docker buildkit externals work hub-mirror; do
  [ -d "$ARC_BLOCK_MOUNT/$d" ] && ok "store dir $d" || bad "store dir $d missing"
done
INST=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)
[ "$INST" -ge 512 ] && ok "fs.inotify.max_user_instances = $INST" \
  || bad "fs.inotify.max_user_instances = $INST (need >=512; kind will fail)"
grep -q "$ARC_BLOCK_MOUNT" /etc/fstab 2>/dev/null \
  && ok "block device in fstab (survives reboot)" || bad "no fstab entry — mount is not persistent"

# ---------------------------------------------------------------- GitHub App
step "GitHub App"
if [ -f "$GITHUB_APP_PRIVATE_KEY" ]; then
  b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
  NOW=$(date +%s)
  H=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  P=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((NOW-60)) $((NOW+540)) "$GITHUB_APP_ID" | b64url)
  S=$(printf '%s.%s' "$H" "$P" | openssl dgst -sha256 -sign "$GITHUB_APP_PRIVATE_KEY" -binary | b64url)
  PERMS=$(curl -s -X POST -H "Authorization: Bearer $H.$P.$S" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/$GITHUB_APP_INSTALLATION_ID/access_tokens" \
    | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin).get("permissions",{})))' 2>/dev/null)
  echo "$PERMS" | grep -q 'organization_self_hosted_runners.*write' \
    && ok "installation grants organization_self_hosted_runners: write" \
    || bad "installation does NOT grant self-hosted runners write. Perms: $PERMS
        (editing App permissions does not apply to existing installs — an org owner must accept)"
  echo "$PERMS" | grep -q '"actions"' \
    && ok "installation grants actions: read (can read job logs)" \
    || echo "  NOTE  no actions:read — CI still works, but you cannot pull job logs from here"
else
  bad "App private key missing at $GITHUB_APP_PRIVATE_KEY"
fi

# ---------------------------------------------------------------- in-pod
step "In-pod invariants (spawning one runner)"
PREV=$(kubectl get autoscalingrunnerset "$RUNNER_SCALE_SET_NAME" -n arc-runners -o jsonpath='{.spec.minRunners}' 2>/dev/null)
kubectl patch autoscalingrunnerset "$RUNNER_SCALE_SET_NAME" -n arc-runners --type=merge \
  -p '{"spec":{"minRunners":1}}' >/dev/null 2>&1
POD=""
for _ in $(seq 1 60); do
  POD=$(kubectl get pods -n arc-runners --no-headers 2>/dev/null | grep 'runner-' | grep '3/3 *Running' | awk '{print $1}' | head -1)
  [ -n "$POD" ] && break; sleep 5
done

if [ -z "$POD" ]; then
  bad "no runner pod reached 3/3 Running in 5min"
else
  ok "runner pod $POD is 3/3 Running"
  chk() { # description, command, expected-substring
    out=$(kubectl exec -n arc-runners "$POD" -c runner -- sh -c "$2" 2>/dev/null)
    echo "$out" | grep -q "$3" && ok "$1 ($out)" || bad "$1 — got: ${out:-<empty>}"
  }
  chk "go present (cross-workspace typecheck needs it)" 'go version' 'go version'

  # GOROOT must NOT be set: actions/setup-go cannot override it, so a global GOROOT
  # makes a different toolchain read this image's stdlib -> "version mismatch" (findings #14)
  GR=$(kubectl exec -n arc-runners "$POD" -c runner -- sh -c 'printenv GOROOT' 2>/dev/null)
  [ -z "$GR" ] && ok "GOROOT unset (actions/setup-go can work)" \
    || bad "GOROOT is set to '$GR' — this breaks every job using actions/setup-go (findings #14)"
  chk "helm is 3.x not 4.x (hosted-image parity)"       'helm version --short' 'v3\.'
  chk "kubectl present"                                  'kubectl version --client=true 2>/dev/null | head -1' 'Client Version'
  chk "libatomic present (Node 26 needs it)"             'ldconfig -p | grep -c libatomic.so.1' '[1-9]'
  chk "/dev/shm > 64M (Chromium stability)"              'df -h /dev/shm | tail -1 | awk "{print \$2}"' '[0-9]G'
  chk "_work on block device, NOT nfs"                   'df -h /home/runner/_work | tail -1 | awk "{print \$1}"' '^/dev/'
  chk "cache redirected to in-cluster server"            'printenv ACTIONS_RESULTS_URL' 'arc-cache'

  # SA token must NOT be mounted, or kubectl silently uses the runner namespace
  if kubectl exec -n arc-runners "$POD" -c runner -- test -f /var/run/secrets/kubernetes.io/serviceaccount/token 2>/dev/null; then
    bad "service account token IS mounted — kubectl in workflows will target the runner namespace (findings #9)"
  else
    ok "no service account token mounted (kubectl defaults to 'default')"
  fi

  # buildkit socket must be group-accessible to uid 1001
  SOCK=$(kubectl exec -n arc-runners "$POD" -c runner -- ls -l /run/buildkit/buildkitd.sock 2>/dev/null)
  echo "$SOCK" | grep -q 'root docker' \
    && ok "buildkitd socket is root:docker (buildx can connect)" \
    || bad "buildkitd socket perms wrong: $SOCK (needs --group=123)"

  # cache server reachable from the pod
  H=$(kubectl exec -n arc-runners "$POD" -c runner -- curl -s -m 10 http://arc-cache.arc-runners.svc.cluster.local:3000/health 2>/dev/null)
  [ "$H" = "healthy" ] && ok "cache server reachable and healthy" || bad "cache server health = ${H:-<no response>}"

  # dockerd must actually be using the hub mirror — check the effect (docker info),
  # not the manifest: a typo'd flag or an old pod would pass a config-only check.
  chk "dockerd registry mirror configured"               'docker info 2>/dev/null | grep -A2 "Registry Mirrors"' 'arc-hub-mirror'

  # hub mirror reachable from the pod (HTTP 200 on the registry API root)
  M=$(kubectl exec -n arc-runners "$POD" -c runner -- curl -s -o /dev/null -w '%{http_code}' -m 10 http://arc-hub-mirror.arc-runners.svc.cluster.local:5000/v2/ 2>/dev/null)
  [ "$M" = "200" ] && ok "hub mirror reachable (HTTP $M)" || bad "hub mirror /v2/ = ${M:-<no response>}"

  # buildkitd must have the mirror config mounted AND loaded
  BK=$(kubectl exec -n arc-runners "$POD" -c buildkitd -- cat /etc/buildkit/buildkitd.toml 2>/dev/null)
  echo "$BK" | grep -q 'arc-hub-mirror' \
    && ok "buildkitd mirror config mounted" || bad "buildkitd config missing arc-hub-mirror (ConfigMap not mounted?)"
fi

kubectl patch autoscalingrunnerset "$RUNNER_SCALE_SET_NAME" -n arc-runners --type=merge \
  -p "{\"spec\":{\"minRunners\":${PREV:-0}}}" >/dev/null 2>&1
echo "  (restored minRunners=${PREV:-0})"

# ---------------------------------------------------------------- summary
step "Summary"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || { echo "  See docs/findings.md for what each check protects against."; exit 1; }
echo "  All good. Trigger a workflow with: runs-on: $RUNNER_SCALE_SET_NAME"
