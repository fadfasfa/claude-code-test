#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
STATE_FILE="$PROJECT_DIR/.ai_workflow/runtime_state_cc.json"
MISSING_ITEMS=()

if [[ "$PROJECT_DIR" == *"各个设定及工作流"* ]]; then
  echo "当前目录错误，请切换到实际项目根目录。" >&2
  exit 2
fi

cat >/dev/null || true

state_query() {
  local query="$1"
  local fallback="$2"
  python - "$STATE_FILE" "$query" "$fallback" <<'PY'
import json, sys
path, query, fallback = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print(fallback)
    raise SystemExit(0)
cur = data
for part in query.split("."):
    if not part:
        continue
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        print(fallback)
        raise SystemExit(0)
if isinstance(cur, bool):
    print("true" if cur else "false")
elif cur is None:
    print(fallback)
else:
    print(cur)
PY
}

state_update_acceptance_ready() {
  python - "$STATE_FILE" <<'PY'
import json, sys, os
path = sys.argv[1]
if not os.path.exists(path):
    raise SystemExit(0)
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
data["acceptance_ready"] = True
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
}

task_scale="$(state_query "task_scale" "small")"
lint_required="$(state_query "lint_required" "false")"
lint_passed="$(state_query "lint_passed" "false")"
tests_passed="$(state_query "tests_passed" "false")"
project_md_synced="$(state_query "project_md_synced" "false")"
project_md_exemption_reason="$(state_query "project_md_exemption_reason" "")"
acceptance_summary_written="$(state_query "acceptance_summary_written" "false")"
workflow_id="$(state_query "workflow_id" "")"

if [[ "$tests_passed" != "true" ]] && \
   [[ ! -f "$PROJECT_DIR/.ai_workflow/test_result.xml" ]] && \
   [[ ! -f "$PROJECT_DIR/.ai_workflow/test_result.json" ]]; then
  MISSING_ITEMS+=("tests: 未满足 tests_passed=true，且缺少 .ai_workflow/test_result.xml/.json")
fi

if [[ "$lint_required" == "true" ]]; then
  if [[ "$lint_passed" != "true" ]] && \
     [[ ! -f "$PROJECT_DIR/.ai_workflow/lint_result.xml" ]] && \
     [[ ! -f "$PROJECT_DIR/.ai_workflow/lint_result.json" ]]; then
    MISSING_ITEMS+=("lint: lint_required=true，但缺少 lint 结果文件且 lint_passed!=true")
  fi
fi

if [[ "$task_scale" == "large" ]]; then
  project_in_diff="false"
  if [[ -n "$(git -C "$PROJECT_DIR" status --porcelain -- PROJECT.md 2>/dev/null || true)" ]]; then
    project_in_diff="true"
  fi
  if [[ "$project_md_synced" != "true" && "$project_in_diff" != "true" && -z "$project_md_exemption_reason" ]]; then
    MISSING_ITEMS+=("PROJECT.md: large 任务必须同步 PROJECT.md，或提供非空 project_md_exemption_reason")
  fi
fi

acceptance_file=""
if [[ -n "$workflow_id" ]]; then
  acceptance_file="$PROJECT_DIR/.ai_workflow/acceptance_summary_${workflow_id}.md"
fi

if [[ "$acceptance_summary_written" != "true" ]]; then
  if [[ -z "$acceptance_file" || ! -f "$acceptance_file" ]]; then
    MISSING_ITEMS+=("acceptance summary: acceptance_summary_written!=true，且缺少 acceptance_summary_${workflow_id}.md")
  fi
fi

if [[ ${#MISSING_ITEMS[@]} -gt 0 ]]; then
  echo "[ACCEPTANCE-BLOCKED]" >&2
  for item in "${MISSING_ITEMS[@]}"; do
    echo "- ${item}" >&2
  done
  exit 2
fi

state_update_acceptance_ready
exit 0
