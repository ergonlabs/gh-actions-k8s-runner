# Runner image for the ergonlabs-k8s scale set.
#
# WHY THIS EXISTS
#   ghcr.io/actions/actions-runner is deliberately minimal and does NOT ship
#   libatomic1. Node 26 (installed by actions/setup-node) links against it, so
#   every job that touched node died with:
#
#     /home/runner/_work/_tool/node/26.1.0/x64/bin/node: error while loading
#     shared libraries: libatomic.so.1: cannot open shared object file
#
#   That took down ergonlabs/sugarmaple-app PR #126 on 2026-08-13: `pnpm install`
#   exited 127, and in the other job `Get pnpm store directory` returned empty,
#   which made actions/cache fail with "Input required and not supplied: path".
#
#   GitHub's real ubuntu-latest is a ~30 GB VM image from actions/runner-images
#   and is not consumable as a container, so the supported approach is to extend
#   the official ARC runner image with the packages you actually need.
FROM ghcr.io/actions/actions-runner:latest

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
      # the one that broke us — Node 26+ needs it
      libatomic1 \
      # compiling native node modules (node-gyp, better-sqlite3, etc.)
      build-essential \
      python3 \
      # actions/cache uses zstd when available; without it caching is slower
      zstd \
      # routinely assumed present by actions and scripts
      unzip \
      zip \
      jq \
      git-lfs \
      curl \
      wget \
      ca-certificates \
      gnupg \
      openssh-client \
    && git lfs install --system \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Kubernetes toolchain.
#
# Versions deliberately MATCH GitHub's hosted ubuntu-24.04 image
# (actions/runner-images images/ubuntu/Ubuntu2404-Readme.md) so a workflow behaves
# the same here as on ubuntu-latest. In particular helm is pinned to 3.x, NOT the
# current 4.x — Helm 4 has breaking changes and a workflow written against
# ubuntu-latest today expects Helm 3.
#
# The base image already provides docker + buildx v0.35.0, which also matches.
# Deliberately NOT installed: minikube, podman, buildah, skopeo — large, and
# redundant here since every runner pod already has dind and buildkitd sidecars.
ARG HELM_VERSION=3.21.3
ARG KUBECTL_VERSION=1.36.2
ARG KUSTOMIZE_VERSION=5.8.1
ARG KIND_VERSION=0.32.0
ARG YQ_VERSION=4.53.3
ARG GO_VERSION=1.26.5

RUN set -eux; \
    cd /tmp; \
    # helm — checksum published by the vendor, so verify it
    curl -fsSL -o helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz"; \
    echo "15e041a93a590dce8100f39385cd98c84a765c9e36aeeb9e2dc6ff9e4769e2e0  helm.tgz" | sha256sum -c -; \
    tar xzf helm.tgz linux-amd64/helm --strip-components=1; \
    install -m 0755 helm /usr/local/bin/helm; \
    \
    # kubectl — checksum published by the vendor, so verify it
    curl -fsSL -o kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl"; \
    echo "1e9045ec32bea85da43de85f0065358529ea7c7a152eca78154fba5b58c27d82  kubectl" | sha256sum -c -; \
    install -m 0755 kubectl /usr/local/bin/kubectl; \
    \
    curl -fsSL -o kustomize.tgz "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_amd64.tar.gz"; \
    tar xzf kustomize.tgz kustomize; \
    install -m 0755 kustomize /usr/local/bin/kustomize; \
    \
    # kind can create throwaway clusters in CI using the dind sidecar
    curl -fsSL -o kind "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-amd64"; \
    install -m 0755 kind /usr/local/bin/kind; \
    \
    curl -fsSL -o yq "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_amd64"; \
    install -m 0755 yq /usr/local/bin/yq; \
    \
    rm -rf /tmp/helm.tgz /tmp/helm /tmp/kubectl /tmp/kustomize.tgz /tmp/kustomize /tmp/kind /tmp/yq; \
    helm version --short; kubectl version --client=true 2>/dev/null | head -1; \
    kustomize version; kind version; yq --version

# ---------------------------------------------------------------------------
# Go.
#
# GitHub's hosted ubuntu-24.04 image has Go on PATH by default, and workflows
# rely on that even when they don't run actions/setup-go. sugarmaple-app's
# packages/eslint-plugin test (acc_ts_strict_mode.spec.ts) runs `pnpm -r typecheck`
# across all 32 workspaces; apps/git-service's typecheck is `go vet ./...`, so
# without Go on PATH that job failed on 2026-08-13 with:
#
#     > go vet ./...
#     sh: 1: go: not found
#
# 350/351 tests passed — the only failure was the missing toolchain. Note the
# dedicated "git-service Go quality gates" job passed, because it *does* use
# actions/setup-go; only the cross-workspace typecheck depended on the default Go.
#
# 1.26.5 is the newest of the three Go versions the hosted image caches
# (1.24.13 / 1.25.12 / 1.26.5), i.e. what `go` resolves to on ubuntu-latest.
RUN set -eux; \
    cd /tmp; \
    curl -fsSL -o go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz"; \
    echo "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053  go.tgz" | sha256sum -c -; \
    tar -C /usr/local -xzf go.tgz; \
    rm -f go.tgz; \
    ln -s /usr/local/go/bin/go /usr/local/bin/go; \
    ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt; \
    go version
#
# DO NOT set ENV GOROOT here.
#
# Go infers GOROOT from the location of its own binary (it resolves the symlink),
# so it is unnecessary. Worse, a global GOROOT is INHERITED by actions/setup-go,
# which installs a different Go onto PATH but cannot override an explicitly-set
# GOROOT. The new toolchain then reads this image's standard library and every
# build dies with:
#
#   compile: version "go1.26.5" does not match go tool version "go1.26.3"
#
# That broke apps/git-service's golangci-lint on 2026-08-13 — introduced by an
# earlier version of this very file. GitHub's hosted images do not set GOROOT
# globally either; setup-go manages it per job.

# ---------------------------------------------------------------------------
# Redirect actions/cache to the in-cluster cache server.
#
# Setting ACTIONS_RESULTS_URL as a container env var is NOT enough: the runner
# overwrites it from the job message it receives from GitHub, so actions/cache
# keeps talking to GitHub's hosted cache. Verified 2026-08-13 — env var was set
# correctly on the pod and a job still restored a 207 MB cache from GitHub while
# our server logged zero requests.
#
# Upstream's fix is to rename the string inside Runner.Worker.dll so the runner
# writes a variable nobody reads, leaving our ACTIONS_RESULTS_URL intact.
# "ACTIONS_RESULTS_URL" -> "ACTIONS_RESULTS_ORL" is the same byte length, so the
# assembly stays valid.
#
# NOTE: upstream documents this as a `sed` on the DLL. That does NOT work here —
# on runner 2.336.0 the literal is stored as UTF-16LE, so an ASCII sed matches
# nothing and silently produces an unpatched image. Hence the explicit UTF-16LE
# byte replacement below, with a hard assert on the occurrence count so a future
# runner version fails the BUILD instead of shipping broken caching.
#
# ARC already sets DisableUpdate=True in its JIT config, so the runner will not
# self-update and undo this at job start.
RUN python3 - <<'PY'
p = "/home/runner/bin/Runner.Worker.dll"
old = "ACTIONS_RESULTS_URL".encode("utf-16-le")
new = "ACTIONS_RESULTS_ORL".encode("utf-16-le")
assert len(old) == len(new), "patch must preserve length"
d = open(p, "rb").read()
n = d.count(old)
assert n == 1, f"expected exactly 1 UTF-16LE occurrence in {p}, found {n} — runner internals changed, re-check the patch"
open(p, "wb").write(d.replace(old, new))
print(f"patched {p}: {n} occurrence")
PY

USER runner
