#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
STATE_FILE="$PROJECT_DIR/.ai_workflow/runtime_state_cc.json"

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

clarify_status="$(state_query "clarify_status" "resolved")"
visual_frozen="$(state_query "visual_frozen" "true")"
task_mode="$(state_query "task_mode" "standard")"
ui_heavy="$(state_query "ui_heavy" "false")"

if [[ "$clarify_status" != "resolved" ]]; then
  echo "[PLAN-EXIT-GUARD] clarify_status=${clarify_status}，需求澄清尚未闭环。" >&2
  exit 2
fi

if [[ "$ui_heavy" == "true" && "$task_mode" == "visual-explore" ]]; then
  if [[ "$visual_frozen" != "true" ]]; then
    echo "[PLAN-EXIT-GUARD] 当前为 UI-heavy 任务，视觉方案尚未冻结。" >&2
    echo "请先完成 visual-explore，再退出 Plan Mode。" >&2
    exit 2
  fi
fi

exit 0
