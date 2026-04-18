#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1

PHASE="${1#--phase=}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
WORKFLOW_DIR="$PROJECT_DIR/.ai_workflow"
STATE_FILE="$WORKFLOW_DIR/runtime_state_cc.json"
EVENT_LOG="$WORKFLOW_DIR/event_log_cc.jsonl"

if [[ "$PROJECT_DIR" == *"各个设定及工作流"* ]] || [[ "$(pwd)" == *"各个设定及工作流"* ]]; then
  echo "当前目录错误，请切换到实际项目根目录。" >&2
  exit 2
fi

mkdir -p "$WORKFLOW_DIR"

if [[ ! -f "$STATE_FILE" ]]; then
  cat >"$STATE_FILE" <<'JSON'
{
  "schema_version": "7.2",
  "endpoint": "cc",
  "workflow_id": "",
  "task_type": "code",
  "task_scale": "small",
  "execution_mode": "ad-hoc",
  "branch_policy": "on-demand",
  "branch_resolution": "on-demand",
  "completion_mode": "local-main",
  "task_mode": "standard",
  "clarify_status": "resolved",
  "ui_heavy": false,
  "visual_frozen": true,
  "acceptance_ready": false,
  "tests_passed": false,
  "lint_required": false,
  "lint_passed": false,
  "project_md_synced": false,
  "acceptance_summary_written": false,
  "branch_ready": false,
  "headless_question_pending": false,
  "kb_access_mode": "file-search",
  "execution_status": "idle",
  "current_stage": "requirement-refine",
  "awaiting_user_input": false,
  "blocked_reason": "",
  "allow_midrun_pause": false,
  "stage_done": false,
  "interface_summary": {
    "modified_symbols": [],
    "affected_files": []
  }
}
JSON
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

read_state() {
  python - "$STATE_FILE" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
print(json.dumps(data, ensure_ascii=False))
PY
}

append_event() {
  local message="$1"
  python - "$EVENT_LOG" "$PHASE" "$message" <<'PY'
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

PROMPT="$(json_query "prompt" "${CLAUDE_USER_PROMPT:-}")"
COMMAND_INPUT="$(json_query "tool_input.command" "${CLAUDE_TOOL_INPUT:-}")"
ASSISTANT_MESSAGE="$(json_query "assistant_message" "${CLAUDE_LAST_ASSISTANT_MESSAGE:-}")"
STATE_JSON="$(read_state)"

UPDATED_STATE="$(
STATE_JSON_ENV="$STATE_JSON" python - "$PHASE" "$PROMPT" "$COMMAND_INPUT" "$ASSISTANT_MESSAGE" <<'PY'
import json, sys, re

phase, prompt, command, assistant_message = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
state = json.loads(__import__("os").environ["STATE_JSON_ENV"])
additional = []

def has_any(text, keywords):
    return any(k in text for k in keywords)

def apply(d):
    state.update(d)

def normalize(text):
    return (text or "").strip()

def infer_stage(text, current_stage):
    if not text:
        return current_stage
    stage_rules = [
        ("data-capture", ["抓取", "下载", "拉取", "爬取", "图标", "资源"]),
        ("rebuild", ["重建 manifests", "重建manifest", "rebuild", "重新生成", "生成前端展示数据"]),
        ("verify", ["验证页面", "验证前端", "检查页面", "verify"]),
        ("implementation", ["开始实现", "改代码", "接入", "写入文件", "处理数据", "直接改", "继续做", "继续第2步"]),
        ("plan-draft", ["计划", "plan draft", "方案", "规划"]),
        ("visual-explore", ["视觉方向", "样板图", "视觉稿", "视觉探索"]),
    ]
    for stage, keywords in stage_rules:
        if has_any(text, keywords):
            return stage
    return current_stage

def set_stage(stage):
    if stage:
        state["current_stage"] = stage

def set_execution_defaults(stage):
    apply({
        "execution_status": "executing",
        "awaiting_user_input": False,
        "blocked_reason": "",
        "allow_midrun_pause": False,
        "stage_done": False,
    })
    set_stage(stage)

def set_blocked(reason, stage=None):
    apply({
        "execution_status": "blocked",
        "awaiting_user_input": True,
        "blocked_reason": reason,
        "allow_midrun_pause": False,
        "stage_done": False,
    })
    set_stage(stage or state.get("current_stage", "requirement-refine"))

def set_planning(stage):
    apply({
        "execution_status": "planning",
        "awaiting_user_input": True,
        "blocked_reason": "",
        "allow_midrun_pause": True,
        "stage_done": False,
    })
    set_stage(stage)

text = normalize(prompt)
assistant_text = normalize(assistant_message)
combined_text = " ".join(part for part in [text, command, assistant_text] if part).strip()

state.setdefault("execution_status", "idle")
state.setdefault("current_stage", "requirement-refine")
state.setdefault("awaiting_user_input", False)
state.setdefault("blocked_reason", "")
state.setdefault("allow_midrun_pause", False)
state.setdefault("stage_done", False)

retrieval_kw = ["查资料", "搜索", "最新案例", "官方文档", "成熟做法", "对比资料"]
code_kw = ["改代码", "修复", "实现", "重构", "新增", "修改", "脚本", "hook", "文件"]
small_kw = ["小修", "小改", "改文案", "改样式", "按钮文案", "微调", "小范围", "≤3 文件", "<=3 文件"]
large_kw = ["架构", "重构", "大改", "整体", "系统", "迁移", "重写", "接口", "数据契约", "多模块", "规划", "整个", "稳定"]
resolved_kw = ["需求已确认", "澄清完成", "按以上执行", "可以进入计划", "输出 Plan Draft"]
visual_frozen_kw = ["选方案 A", "选方案 B", "选方案 C", "选第", "视觉方案确认", "按这个图做", "方案冻结"]
implement_kw = ["开始实现", "可以做了", "签发", "输出 File Change Spec", "HANDOFF-WRITE", "按计划执行"]
branch_kw = ["开分支", "建分支", "提 PR", "push", "发起评审"]
ui_positive_kw = ["样板图", "视觉稿", "概念图", "多个界面方向", "布局方向供选择", "先看图再决定", "视觉探索", "样式方案比选", "前端样式方案比选", "几个前端样板图方向"]
ui_negative_kw = ["图标抓取", "素材抓取", "数据抓取", "资源下载", "文案替换", "一般前端实现", "API 接入", "真实图片", "真实图标", "图标资源整理", "资源整理", "抓取真实图标", "写入数据文件"]
execution_transition_kw = ["开始实现", "继续执行", "开始抓取", "继续抓取", "按计划执行", "进入第 2 步", "真实图标抓取", "资源抓取", "数据处理"]
execution_prompt_kw = ["直接做", "继续做", "继续第2步", "继续第 2 步", "开始实现", "开始抓取", "继续抓取", "重建", "验证页面", "按计划执行", "直接改", "继续改"]
progress_pause_kw = ["先汇报", "先停一下", "先别继续改", "checkpoint", "progress update", "不要继续改"]
discussion_kw = ["先讨论", "先规划", "先分析", "先给方案", "先出计划", "先别改代码", "讨论任务"]
blocked_reason_rules = [
    ("缺少必要凭据/登录态/网络访问", ["缺少凭据", "缺少登录", "未登录", "没有登录态", "无网络", "网络访问失败"]),
    ("目标路径或目标文件不明确，无法安全推断", ["路径不明确", "目标文件不明确", "无法确定目标路径", "无法安全推断"]),
    ("涉及破坏性操作且用户未明确授权", ["破坏性操作", "需要删除", "需要覆盖", "需要重置", "未明确允许"]),
    ("需求仍然多义，继续执行会明显跑偏", ["需求多义", "存在歧义", "方向不明确", "继续执行会跑偏"]),
    ("外部依赖失败多次，无法继续推进", ["依赖失败多次", "多次失败", "外部服务失败", "页面访问失败", "无法继续推进"]),
]
completion_markers = ["已完成", "全部完成", "最终结果", "验证通过", "Acceptance Summary", "验收已写入", "抓取完成", "重建完成", "页面验证完成", "基本检查完成", "测试通过"]
stage_completion_rules = {
    "data-capture": ["抓取完成", "目标文件已落盘", "下载完成", "资源已落盘"],
    "rebuild": ["manifest 已重建", "manifests 已重建", "normalized 数据已重建", "重建完成"],
    "verify": ["页面验证完成", "验证通过", "verify 完成", "检查完成"],
    "implementation": ["代码改动完成", "基本检查完成", "测试通过", "lint 通过", "实现完成"],
    "acceptance": ["验收已写入", "Acceptance Summary", "最终结果"],
}

def is_ui_explore_task(text):
    if not text:
        return False
    return has_any(text, ui_positive_kw)

def is_non_visual_execution(text):
    if not text:
        return False
    return has_any(text, execution_transition_kw) or has_any(text, ui_negative_kw)

def clear_visual_block():
    state["ui_heavy"] = False
    state["visual_frozen"] = True
    if state.get("task_mode") == "visual-explore":
        state["task_mode"] = "standard"

def maybe_mark_stage_done(text):
    current_stage = state.get("current_stage", "requirement-refine")
    if current_stage == "acceptance" and has_any(text, completion_markers):
        apply({"stage_done": True, "execution_status": "completed", "awaiting_user_input": False, "blocked_reason": ""})
        return
    for stage, markers in stage_completion_rules.items():
        if current_stage == stage and has_any(text, markers):
            apply({"stage_done": True, "blocked_reason": ""})
            if stage == "acceptance":
                state["execution_status"] = "completed"
            return

def find_blocked_reason(text):
    for reason, markers in blocked_reason_rules:
        if has_any(text, markers):
            return reason
    return ""

if phase == "prompt-submit":
    if has_any(text, retrieval_kw) and not has_any(text, code_kw):
        apply({
            "task_type": "retrieval",
            "execution_mode": "ad-hoc",
            "branch_policy": "none",
            "branch_resolution": "none",
            "completion_mode": "local-main",
            "clarify_status": "resolved",
            "visual_frozen": True,
            "ui_heavy": False,
            "task_mode": "standard",
        })

    is_ui = is_ui_explore_task(text) and not is_non_visual_execution(text)
    is_large = has_any(text, large_kw)
    is_small = has_any(text, small_kw) and not is_large
    should_clear_visual = is_non_visual_execution(text) and not is_ui

    if is_small:
        apply({
            "task_type": "code",
            "task_scale": "small",
            "execution_mode": "ad-hoc",
            "branch_policy": "on-demand",
            "branch_resolution": "on-demand",
            "completion_mode": "local-main",
            "clarify_status": "resolved",
            "visual_frozen": True,
            "ui_heavy": False,
            "task_mode": "standard",
        })

    if is_large:
        apply({
            "task_type": "code",
            "task_scale": "large",
            "execution_mode": state.get("execution_mode", "ad-hoc") if state.get("execution_mode") == "contract" else "ad-hoc",
            "branch_policy": state.get("branch_policy", "on-demand") if state.get("branch_policy") == "required" else "on-demand",
            "branch_resolution": state.get("branch_resolution", "on-demand") if state.get("branch_resolution") == "required" else "on-demand",
            "completion_mode": state.get("completion_mode", "local-main") if state.get("completion_mode") == "PR-merge" else "local-main",
        })
        if has_any(text, resolved_kw):
            state["clarify_status"] = "resolved"
        else:
            state["clarify_status"] = "pending"
            additional.append("当前任务疑似 large 且存在多义性；先用 3–7 个高价值问题澄清，不要输出 Plan Draft / Validation Draft / File Change Spec / 代码。")

    if is_ui:
        apply({
            "ui_heavy": True,
            "visual_frozen": False,
            "task_mode": "visual-explore",
        })
        additional.append("当前任务为 UI-heavy；必须先 visual-explore。若支持 Nano Banana，先出图；若不支持，输出 [VISUAL-DEGRADED]。")
    elif should_clear_visual:
        clear_visual_block()

    if has_any(text, visual_frozen_kw):
        state["visual_frozen"] = True
        if state.get("task_mode") == "visual-explore":
            state["ui_heavy"] = False
            state["task_mode"] = "standard"

    if state.get("task_scale") == "large" and has_any(text, implement_kw):
        apply({
            "execution_mode": "contract",
            "branch_policy": "required",
            "branch_resolution": "required",
            "completion_mode": "PR-merge",
            "clarify_status": "resolved",
        })
        clear_visual_block()

    if has_any(text, branch_kw):
        apply({
            "branch_policy": "required",
            "branch_resolution": "required",
            "completion_mode": "PR-merge",
        })

    stage = infer_stage(text, state.get("current_stage", "requirement-refine"))
    explicit_blocker = find_blocked_reason(text)
    wants_progress_pause = has_any(text, progress_pause_kw)
    wants_discussion = has_any(text, discussion_kw) or stage in {"plan-draft", "visual-explore"}
    wants_execution = has_any(text, execution_prompt_kw) or stage in {"implementation", "data-capture", "rebuild", "verify"}

    if explicit_blocker:
        set_blocked(explicit_blocker, stage)
    elif wants_progress_pause:
        apply({
            "execution_status": "executing" if state.get("execution_status") == "executing" else "planning",
            "awaiting_user_input": True,
            "blocked_reason": "",
            "allow_midrun_pause": True,
        })
        set_stage(stage)
    elif wants_execution:
        set_execution_defaults(stage)
    elif wants_discussion:
        set_planning(stage)
    else:
        set_stage(stage)

if phase == "bash" and command:
    if "各个设定及工作流" in command:
        additional.append("检测到命令试图访问备份目录；真正阻断由 PATH guard 和 permissions.deny 执行。")
    command_stage = infer_stage(command, state.get("current_stage", "requirement-refine"))
    if command_stage != state.get("current_stage"):
        set_stage(command_stage)
    if state.get("execution_status") == "blocked":
        pass
    elif state.get("execution_status") == "executing":
        state["awaiting_user_input"] = False
        state["allow_midrun_pause"] = False
        state["blocked_reason"] = ""
    maybe_mark_stage_done(command)

if phase in {"write", "edit", "multiedit"}:
    if state.get("task_scale") == "large" and state.get("execution_mode") == "contract":
        state["branch_resolution"] = state.get("branch_resolution", "required")
    if state.get("current_stage") in {"implementation", "data-capture", "rebuild", "verify"}:
        apply({
            "execution_status": "executing",
            "awaiting_user_input": False,
            "blocked_reason": "",
            "allow_midrun_pause": False,
        })
        state["stage_done"] = False

if state.get("ui_heavy") is True and state.get("visual_frozen") is False and is_non_visual_execution(text):
    clear_visual_block()

if assistant_text:
    maybe_mark_stage_done(assistant_text)
    if state.get("execution_status") != "blocked":
        blocked = find_blocked_reason(assistant_text)
        if blocked:
            set_blocked(blocked, state.get("current_stage"))

if state.get("execution_status") == "completed":
    state["awaiting_user_input"] = False
    state["allow_midrun_pause"] = False
    state["blocked_reason"] = ""

if state.get("execution_status") == "executing" and state.get("blocked_reason"):
    state["blocked_reason"] = ""

if state.get("execution_status") == "executing" and state.get("awaiting_user_input") is True and state.get("allow_midrun_pause") is False:
    state["awaiting_user_input"] = False

if state.get("current_stage") == "acceptance" and state.get("stage_done"):
    state["execution_status"] = "completed"

print(json.dumps({"state": state, "additional": additional}, ensure_ascii=False))
PY
)"

python - "$STATE_FILE" "$UPDATED_STATE" <<'PY'
import json, sys
path, payload = sys.argv[1], sys.argv[2]
data = json.loads(payload)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data["state"], fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY

if [[ "$PHASE" == "bash" && "$COMMAND_INPUT" == *"各个设定及工作流"* ]]; then
  append_event "[PATH-WARN] $COMMAND_INPUT"
fi

python - "$UPDATED_STATE" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
for item in payload.get("additional", []):
    print(item)
PY

exit 0
