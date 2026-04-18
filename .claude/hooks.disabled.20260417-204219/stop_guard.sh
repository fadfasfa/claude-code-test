#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
STATE_FILE="$PROJECT_DIR/.ai_workflow/runtime_state_cc.json"
LAST_MESSAGE="${CLAUDE_LAST_ASSISTANT_MESSAGE:-}"

if [[ "$PROJECT_DIR" == *"各个设定及工作流"* ]]; then
  echo "当前目录错误，请切换到实际项目根目录。" >&2
  exit 2
fi

cat >/dev/null || true

python - "$STATE_FILE" "$LAST_MESSAGE" <<'PY'
import json
import os
import re
import sys

state_path, last_message = sys.argv[1], sys.argv[2]

try:
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
except Exception:
    raise SystemExit(0)

def truthy(value):
    return value is True or str(value).lower() == "true"

def has_any(text, phrases):
    return any(phrase in text for phrase in phrases)

execution_status = state.get("execution_status", "idle")
current_stage = state.get("current_stage", "requirement-refine")
awaiting_user_input = truthy(state.get("awaiting_user_input", False))
allow_midrun_pause = truthy(state.get("allow_midrun_pause", False))
stage_done = truthy(state.get("stage_done", False))
blocked_reason = (state.get("blocked_reason") or "").strip()
acceptance_ready = truthy(state.get("acceptance_ready", False))
acceptance_summary_written = truthy(state.get("acceptance_summary_written", False))
all_todos_done = truthy(state.get("all_todos_done", False))
clarify_status = state.get("clarify_status", "resolved")
visual_frozen = truthy(state.get("visual_frozen", True))
ui_heavy = truthy(state.get("ui_heavy", False))
task_scale = state.get("task_scale", "small")
task_mode = state.get("task_mode", "standard")

summary_markers = [
    "下一步我会",
    "我接下来",
    "确认",
    "继续把这段收完",
    "我已经继续推进",
    "现状如下",
]
final_markers = [
    "已完成",
    "全部完成",
    "最终结果",
    "验证通过",
    "Acceptance Summary",
]

message = (last_message or "").strip()
midrun_summary = (
    execution_status == "executing"
    and has_any(message, summary_markers)
    and not has_any(message, final_markers)
)

if current_stage == "acceptance" or acceptance_ready or acceptance_summary_written:
    raise SystemExit(0)

if all_todos_done:
    raise SystemExit(0)

if execution_status == "blocked" and blocked_reason:
    raise SystemExit(0)

if stage_done or awaiting_user_input or allow_midrun_pause:
    raise SystemExit(0)

should_block_continue = (
    execution_status == "executing"
    and not awaiting_user_input
    and not blocked_reason
    and not allow_midrun_pause
    and not stage_done
)

if should_block_continue or midrun_summary:
    print("[CONTINUE-GUARD] 当前为连续执行阶段，未遇到真实阻断，请继续完成本阶段，不要在中途总结后停止。", file=sys.stderr)
    raise SystemExit(2)

missing_items = []
if clarify_status == "pending":
    missing_items.append("clarify_status=pending")

if ui_heavy and task_mode == "visual-explore" and not visual_frozen:
    missing_items.append("ui_heavy=true 且 visual_frozen!=true")

if task_scale == "large":
    if not acceptance_ready:
        missing_items.append("acceptance_ready!=true")
    if not acceptance_summary_written and not acceptance_ready:
        missing_items.append("acceptance_summary_written!=true")

if missing_items:
    print("[STOP-GUARD] 以下阻断项未处理，禁止结束任务：", file=sys.stderr)
    for item in missing_items:
        print(f"- {item}", file=sys.stderr)
    raise SystemExit(2)

raise SystemExit(0)
PY
