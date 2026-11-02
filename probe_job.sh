#!/usr/bin/env bash
# Runs inside a transient copy of a job's real systemd unit (agent-jobs probe).
# Same user, sandbox, slice, environment and provider drop-in as the job; only
# ExecStart differs. Modes: probe (harmless model calls), canary (the backlog
# runner's own --canary), dry-run (the review runner with REVIEW_DRY_RUN=1 and a
# 2-item limit).
set -uo pipefail
readonly JOB="${1:?job}"
readonly MODE="${2:-probe}"
readonly RUNTIME="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# Site configuration. The probe asks for a fact that only the project's own
# instructions (AGENTS.md or CLAUDE.md) contain, and runs a command that a
# project PreToolUse hook must refuse. Both prove that project settings loaded.
readonly INSTRUCTIONS_QUESTION="the probe marker named in the project instructions"
readonly INSTRUCTIONS_ANSWER="agent-probe-ok"
readonly HOOK_BLOCKED_COMMAND="echo npm run check | cat"

case "${JOB}:${MODE}" in
  backlog:canary)
    exec /usr/bin/python3 /usr/local/libexec/backlog-runner/runner.py --canary ;;
  review:dry-run)
    exec env REVIEW_DRY_RUN=1 REVIEW_LIMIT=2 /srv/agent/work/review/scripts/run-review.sh code ;;
  *:probe) ;;
  *) echo "unsupported probe mode ${MODE} for ${JOB}" >&2; exit 2 ;;
esac

case "${JOB}" in
  backlog)
    bin="${CODEX_BIN:?}"; checkout="${BACKLOG_CHECKOUT:-/srv/agent/work/backlog}"
    sandbox="${BACKLOG_CODEX_SANDBOX:-workspace-write}"; model="${BACKLOG_MODEL:?}"
    effort="${BACKLOG_REASONING:-high}"
    schemas=(/usr/local/libexec/backlog-runner/select-output.schema.json
             /usr/local/libexec/backlog-runner/implement-output.schema.json
             /usr/local/libexec/backlog-runner/review-output.schema.json) ;;
  review)
    bin="${CODEX_BIN:?}"; checkout=/srv/agent/work/review
    sandbox=read-only; model="${REVIEW_MODEL:?}"; effort="${REVIEW_REASONING:-xhigh}"
    schemas=("${checkout}/scripts/review-output.schema.json") ;;
  *) echo "unknown job ${JOB}" >&2; exit 2 ;;
esac

work="$(mktemp -d "${TMPDIR:-/tmp}/agent-provider-probe.XXXXXX")"
trap 'rm -rf -- "${work}"' EXIT
echo "probe ${JOB}: user=$(id -un) cwd=${checkout} sandbox=${sandbox} model=${model} effort=${effort}"
echo "probe ${JOB}: agent=$("${bin}" --version)"

common=(exec --ephemeral --ignore-user-config --disable apps --disable plugins --sandbox "${sandbox}"
        --model "${model}" -c 'approval_policy="never"' --cd "${checkout}")
status=0

cat > "${work}/prompt.txt" <<PROMPT
This is an automated configuration probe of a scheduled job. Do not modify any file.
1. Without using a tool, state ${INSTRUCTIONS_QUESTION} (field instructions_fact).
2. If a Bash tool is available, run exactly: ${HOOK_BLOCKED_COMMAND}   and set hook_blocked=true only if a hook refused it; otherwise set hook_blocked=false.
3. If a Bash tool is available, run exactly: git status --short | wc -l   and report the number as dirty_lines; otherwise report -1.
4. List the names of the tools available to you in tools_seen.
Return the structured result.
PROMPT
"${bin}" "${common[@]}" -c "model_reasoning_effort=\"${effort}\"" \
  --output-schema "${RUNTIME}/probe.schema.json" --output-last-message "${work}/probe.json" - \
  < "${work}/prompt.txt" > "${work}/probe.stdout" || status=$?
if [[ -s "${work}/probe.json" ]]; then
  echo "probe ${JOB}: config result $(cat "${work}/probe.json")"
  python3 - "${work}/probe.json" "${sandbox}" "${INSTRUCTIONS_ANSWER}" <<'PY' || status=1
import json, sys
result = json.load(open(sys.argv[1]))
read_only = sys.argv[2] == "read-only"
problems = []
if result["instructions_fact"] != sys.argv[3]:
    problems.append("project instructions (AGENTS.md) were not loaded")
if not read_only and not result["hook_blocked"]:
    problems.append("PreToolUse hook did not block the probe command")
if read_only and "Bash" in result["tools_seen"]:
    problems.append("read-only job exposes Bash")
if problems:
    print("probe FAILED: " + "; ".join(problems))
    sys.exit(1)
print("probe config: ok")
PY
else
  echo "probe ${JOB}: no config result (adapter exit ${status})"; status=1
fi

for schema in "${schemas[@]}"; do
  name="$(basename "${schema}" .json)"
  printf '%s\n' "Configuration probe. Do not use any tool and do not modify anything." \
    "Return the smallest valid object for the required schema that honestly describes a no-op probe:" \
    "use neutral or blocked statuses, empty arrays where allowed, zero counts, and the word probe in text fields." \
    > "${work}/schema-prompt.txt"
  if "${bin}" "${common[@]}" -c 'model_reasoning_effort="low"' --output-schema "${schema}" \
      --output-last-message "${work}/${name}.json" - < "${work}/schema-prompt.txt" > /dev/null; then
    echo "probe ${JOB}: schema ${name}: accepted and validated ($(wc -c < "${work}/${name}.json") bytes)"
  else
    echo "probe ${JOB}: schema ${name}: FAILED"; status=1
  fi
done
echo "probe ${JOB}: exit ${status}"
exit "${status}"
