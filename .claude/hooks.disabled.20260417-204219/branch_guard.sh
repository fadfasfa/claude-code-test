#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1

PHASE="${1#--phase=}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
STATE_FILE="$PROJECT_DIR/.ai_workflow/runtime_state_cc.json"

if [[ "$PROJECT_DIR" == *"各个设定及工作流"* ]]; then
  echo "当前目录错误，请切换到实际项目根目录。" >&2
  exit 2
fi

HOOK_INPUT="$(cat || true)"

json_query() {
  local query="$1"
  local fallback="${2:-}"
  HOOK_JSON="$HOOK_INPUT" python - "$query" "$fallback" <<'PY'
import json, sys
query = sys.argv[1].split(".")
fallback = sys.argv[2]
raw = __import__("os").environ.get("HOOK_JSON", "").strip()
if not raw:
    print(fallback)
    raise SystemExit(0)
try:
    data = json.loads(raw)
except Exception:
    print(fallback)
    raise SystemExit(0)
cur = data
for part in query:
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

append_event() {
  local message="$1"
  mkdir -p "$PROJECT_DIR/.ai_workflow"
  python - "$PROJECT_DIR/.ai_workflow/event_log_cc.jsonl" "$PHASE" "$message" <<'PY'
import json, sys, datetime
path, phase, message = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {
    "ts": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(),
    "endpoint": "cc",
    "phase": phase,
    "message": message,
}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
PY
}

update_branch_ready() {
  local branch_name="$1"
  python - "$STATE_FILE" "$branch_name" <<'PY'
import json, sys, os
path, branch_name = sys.argv[1], sys.argv[2]
if not os.path.exists(path):
    raise SystemExit(0)
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
data["branch_ready"] = True
data["branch_name"] = branch_name
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
}

task_scale="$(state_query "task_scale" "small")"
execution_mode="$(state_query "execution_mode" "ad-hoc")"
branch_policy="$(state_query "branch_policy" "on-demand")"
workflow_id="$(state_query "workflow_id" "")"
command_from_json="$(json_query "tool_input.command" "")"
command_input="${command_from_json:-${CLAUDE_TOOL_INPUT:-}}"
current_branch="$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"

if [[ "$PHASE" == "bash" ]]; then
  if [[ "$command_input" == *"C:/Users/apple/OneDrive/Desktop/各个设定及工作流"* ]] || \
     [[ "$command_input" == *"/c/Users/apple/OneDrive/Desktop/各个设定及工作流"* ]] || \
     [[ "$command_input" == *"各个设定及工作流"* ]]; then
    append_event "[PATH-GUARD] blocked backup path command: $command_input"
    echo "[PATH-GUARD] 当前项目运行时禁止访问工作流备份库。该目录仅用于人工备份和查阅。" >&2
    exit 2
  fi

  if [[ "$branch_policy" == "on-demand" || "$branch_policy" == "none" ]]; then
    if printf '%s' "$command_input" | grep -Eq '(^|[[:space:]])git[[:space:]]+(checkout[[:space:]]+-b|switch[[:space:]]+-c)|gh[[:space:]]+pr[[:space:]]+create|git[[:space:]]+push[[:space:]]+-u'; then
      echo "[BRANCH-GUARD] 当前任务 branch_policy=${branch_policy}，未收到用户显式升格指令。" >&2
      echo "请先让用户明确要求开分支、建分支、提 PR、push 或发起评审。" >&2
      exit 2
    fi
  fi

  exit 0
fi

if [[ "$PHASE" == "write" ]]; then
  if [[ "$task_scale" == "large" && "$execution_mode" == "contract" ]]; then
    if [[ -z "$workflow_id" ]]; then
      echo "[BRANCH-GUARD] large/contract 任务缺少 workflow_id，禁止继续写入。" >&2
      exit 2
    fi

    if [[ "$current_branch" == "main" || "$current_branch" == "master" ]]; then
      echo "[BRANCH-GUARD] large/contract 任务不得在 ${current_branch} 分支直接写入。" >&2
      exit 2
    fi

    expected_a="cc-task-${workflow_id}"
    expected_b="$workflow_id"
    valid_branch=false

    if [[ "$current_branch" == *"$workflow_id"* ]]; then
      valid_branch=true
    fi
    if [[ "$current_branch" == "$expected_a" ]]; then
      valid_branch=true
    fi
    if [[ "$workflow_id" == cc-task-* && "$current_branch" == "$expected_b" ]]; then
      valid_branch=true
    fi

    if [[ "$valid_branch" != "true" ]]; then
      echo "[BRANCH-GUARD] large/contract 任务必须在 task-owned branch/worktree 上写代码。" >&2
      echo "当前分支: ${current_branch}" >&2
      echo "期望包含 workflow_id，或使用 ${expected_a}" >&2
      exit 2
    fi

    update_branch_ready "$current_branch"
    echo "[BRANCH: REUSED ${current_branch}]"
  fi

  exit 0
fi

exit 0
