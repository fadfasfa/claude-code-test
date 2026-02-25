#!/bin/bash
# 动态环境检测钩子 (Pre-flight Check)
# 核心目标：确保 Qwen 执行修改前，Git 工作区绝对干净

# 检查 git 状态
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: DIRTY_WORKSPACE. 请先提交或清理暂存区。"
  exit 1
fi

# (可选) 清理缓存目录，防止状态污染
# rm -rf node_modules/.cache __pycache__

echo "PASS: Workspace is clean."
exit 0
