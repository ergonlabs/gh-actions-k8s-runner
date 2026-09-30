#!/bin/bash
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED hook (set on the runner container in 20-/21-scale-set.yaml).
#
# Renders this pod's resource report (see render.py and manifests/47-resource-sampler.yaml)
# into the job summary and the "Complete runner" step log. The runner registers this hook with
# an always() condition, so it also runs for failed and cancelled jobs — which are exactly the
# ones worth looking at.
#
# It must NEVER fail the job: a non-zero exit from a job hook fails the whole job. Everything
# is best-effort, time-boxed, and the script always exits 0.
#
# Hooks can't upload artifacts: the runner only hands ACTIONS_RUNTIME_TOKEN to node actions,
# never to scripts (ScriptHandler). The HTML goes up via the resource-report composite action.
out="${RUNNER_TEMP:-/tmp}/resource-report-final"
timeout 120 python3 /opt/resource-report/render.py \
  --out "$out" \
  --summary "${GITHUB_STEP_SUMMARY:-}" \
  || echo "resource-report: skipped (exit $?)"
exit 0
