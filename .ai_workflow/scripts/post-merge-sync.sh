#!/usr/bin/env bash
# =============================================================================
# post-merge-sync.sh — Hextech 本地合并后同步脚本
# version: 5.3
#
# 用途：
#   在 git pull / git merge 后对齐本地 agents.md 为 standby 态。
#   可作为 .git/hooks/post-merge 钩子使用，也可手动执行。
#
# 安装为 git hook（可选）：
#   cp .ai_workflow/scripts/post-merge-sync.sh .git/hooks/post-merge
#   chmod +x .git/hooks/post-merge
#
# 手动运行：
#   bash .ai_workflow/scripts/post-merge-sync.sh
# =============================================================================

set -euo pipefail

AGENTS_FILE="agents.md"
HISTORY_DIR=".ai_workflow/agents_history"
SCRIPT_NAME="post-merge-sync"

log() { echo "[${SCRIPT_NAME}] $*"; }
warn() { echo "[${SCRIPT_NAME}] WARNING: $*" >&2; }

# --- 检查 agents.md 是否存在 ---
if [ ! -f "$AGENTS_FILE" ]; then
  log "agents.md not found, skipping sync."
  exit 0
fi

# --- 读取当前 status ---
current_status=$(grep -E '^status:' "$AGENTS_FILE" | head -n 1 | sed 's/^status:[[:space:]]*//')

if [ "$current_status" = "standby" ]; then
  log "agents.md is already in standby. No action needed."
  exit 0
fi

# --- 读取 task_id（用于归档） ---
task_id=$(grep -E '^task_id:' "$AGENTS_FILE" | head -n 1 | sed 's/^task_id:[[:space:]]*//')
[ -z "$task_id" ] && task_id="unknown-task-$(date +%Y%m%d%H%M%S)"

# --- 归档当前 agents.md ---
mkdir -p "$HISTORY_DIR"
archive_path="${HISTORY_DIR}/local-${task_id}.md"

if [ -f "$archive_path" ]; then
  warn "Archive path already exists: ${archive_path}. Skipping archive to avoid overwrite."
else
  cp "$AGENTS_FILE" "$archive_path"
  log "Archived current agents.md to: ${archive_path}"
fi

# --- 检测远端是否已经回到 standby（优先使用远端版本） ---
remote_status=""
if git rev-parse --git-dir > /dev/null 2>&1; then
  current_branch=$(git branch --show-current 2>/dev/null || echo "main")
  remote_status=$(git show "origin/${current_branch}:${AGENTS_FILE}" 2>/dev/null \
    | grep -E '^status:' | head -n 1 | sed 's/^status:[[:space:]]*//' || true)
fi

if [ "$remote_status" = "standby" ]; then
  # 远端已是 standby，直接覆盖本地
  git checkout "origin/${current_branch}" -- "$AGENTS_FILE" 2>/dev/null || true
  log "Local agents.md synced from remote standby."
  exit 0
fi

# --- 远端未回到 standby，或无法读取远端 —— 生成本地 standby 壳 ---
# 以下结构与 agents_template.md 逐字同构
warn "Remote standby not detected. Generating local standby shell from template."

NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
BASE_BRANCH=$(git branch --show-current 2>/dev/null || echo "main")

cat > "$AGENTS_FILE" <<STANDBY_EOF
## 工作范围 (agents.md) — Hextech
> version: 5.3
> status: standby
> 本文件为长期任务总表；待机时保留最近一次合并的归档指针，供下次任务复用。
> retrieval 任务默认不生成代码契约，但可按需写入轻量记录。

---

## 任务头（受控字段）

task_id: none
task_type: code
execution_mode: ad-hoc
branch_policy: on-demand
branch_name: none
base_branch: ${BASE_BRANCH}
project_path: [待填写]
executor: [待填写]
status: standby
created_at: ${NOW}
last_updated_at: ${NOW}
current_branch: none
current_review_path: none

Task_Mode: standard
Contributors:
  - [待填写]

---

## 初始范围（尽量不改）

initial_target_files:
  - none

initial_modified_symbols:
  - none

initial_goals:
  - [待填写]

---

## 当前有效范围（允许覆盖）

effective_target_files:
  - none

effective_modified_symbols:
  - none

effective_goals:
  - [待填写]

---

## 运行台账（追加式）

execution_ledger:
  - ts: ${NOW}
    type: RESET_TO_STANDBY
    summary: "本地 post-merge-sync 执行后回到 standby（前任务: ${task_id}）"
    files:
      - none

---

## Decision_Validation

Final_Signer: self
Validation_Sources:
  - none
Validation_Result: skipped
Human_Validation_Required: no
Human_Validation_Reason: none

---

## 待机态（可复用壳）

active_task_id: none
last_merged_task_id: ${task_id}
last_merged_at: ${NOW}
default_base_branch: ${BASE_BRANCH}
default_branch_policy_for_ad_hoc: on-demand
merge_archive_path: ${archive_path}
STANDBY_EOF

log "Local agents.md reset to standby. Archive: ${archive_path}"

# --- 清理已合并的本地分支 ---
log "Cleaning up merged local branches..."
git branch --merged "${BASE_BRANCH}" 2>/dev/null \
  | grep -vE '^\*|main|master|develop' \
  | while read -r branch; do
      branch=$(echo "$branch" | xargs)
      if [ -n "$branch" ]; then
        git branch -d "$branch" 2>/dev/null && log "Deleted merged branch: $branch" || true
      fi
    done

log "Post-merge sync complete."
