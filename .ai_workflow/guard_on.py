#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
设置真正的 Git pre-commit 钩子，强制进行 AST 安全扫描与 V3.2 状态机相位门控。
"""

import os
import sys

def setup_git_hook():
    """创建并配置 Git pre-commit 钩子"""
    hook_path = '.git/hooks/pre-commit'

    # 确保 .git/hooks 目录存在
    hook_dir = os.path.dirname(hook_path)
    if not os.path.exists(hook_dir):
        os.makedirs(hook_dir, exist_ok=True)
        print(f"[SUCCESS] Created directory: {hook_dir}")

    # Bash 钩子脚本内容 (V3.2 强化版)
    hook_script = '''#!/bin/bash
# V3.2 Git pre-commit hook: State-Machine Gating & AST security scanning
export PYTHONIOENCODING=utf-8

echo "[GATE] Initiating Tri-layer Defense Scans..."

# ==========================================
# 1. 强制状态机相位门控 (State Machine Gating)
# ==========================================
AGENTS_MD="agents.md"
if [ -f "$AGENTS_MD" ]; then
    if ! grep -q "\\[Node C 审计通过\\]" "$AGENTS_MD"; then
        echo "------------------------------------------------------"
        echo "[FATAL 熔断] 状态机门控拦截！"
        echo "当前 agents.md 尚未达到 [Node C 审计通过] 状态。"
        echo "架构铁律：代码必须由 Roo Code (Node C) 终审覆盖后方可合入！"
        echo "提交已物理中止 (Commit Aborted)。"
        echo "------------------------------------------------------"
        exit 1
    fi
fi

# ==========================================
# 2. 幂等化清理防线验证 (Idempotency Check)
# ==========================================
AUDIT_BUFFER=".ai_workflow/audit_buffer"
if [ -d "$AUDIT_BUFFER" ]; then
    # 查找除了 .gitkeep 之外的所有遗留文件
    remaining_files=$(find "$AUDIT_BUFFER" -type f -not -name ".gitkeep" 2>/dev/null)
    if [ -n "$remaining_files" ]; then
        echo "------------------------------------------------------"
        echo "[FATAL 熔断] 幂等化防线拦截！"
        echo "原因：.ai_workflow/audit_buffer/ 内存在残留增量文件。"
        echo "指令：请确保 Node C 终审后执行了清空隔离缓冲区的动作。"
        echo "------------------------------------------------------"
        exit 1
    fi
fi

# ==========================================
# 3. 基础物理安全扫描 (本地 AST 静态兜底)
# ==========================================
staged_files=$(git diff --cached --name-only | grep '\\.py$')

if [ -z "$staged_files" ]; then
    echo "[SUCCESS] Phase-Gate passed. No Python files to scan."
    exit 0
fi

# 调用 AST 扫描引擎兜底防线
failed=0
while IFS= read -r file; do
    if [ ! -f "$file" ]; then continue; fi
    python -c "
import sys
sys.path.insert(0, '.ai_workflow')
from pre_execution_check import check_dangerous_calls

file_path = r'$file'.replace(chr(92), '/')
try:
    if file_path.startswith('.ai_workflow/') or file_path.startswith('scripts/'):
        sys.exit(0)

    with open(file_path, 'r', encoding='utf-8') as f:
        code = f.read()
    safe, violations = check_dangerous_calls(code, exempted_modules=[])
    if not safe:
        print(f'[HOOK-INTERCEPT] AST intercept [{file_path}]:')
        for v in violations: print(f'   - {v}')
        sys.exit(1)
except Exception as e:
    print(f'[ERROR] Scan crashed {file_path}: {e}')
    sys.exit(1)
" "$file"
    if [ $? -ne 0 ]; then failed=1; fi
done <<< "$staged_files"

if [ $failed -eq 1 ]; then
    echo "[ERROR] 安全防线被击穿，合入中止。"
    exit 1
fi

echo "[SUCCESS] 防线闭环确认无误，合法合入！"
exit 0
'''

    # 写入钩子脚本，使用 \n 作为换行符，防止 Windows CRLF 导致 Bash 解析失败
    with open(hook_path, 'w', newline='\n', encoding='utf-8') as f:
        f.write(hook_script)

    # 赋予执行权限
    os.chmod(hook_path, 0o755)

    print(f"[SUCCESS] Git pre-commit hook created: {hook_path}")
    print(f"[SUCCESS] Hook permissions set to: 0o755")
    print("\n[SUCCESS] Git hook setup completed!")
    print("  All subsequent git commit operations will go through V3.2 State-Machine gating & AST security scan")

if __name__ == '__main__':
    try:
        setup_git_hook()
        sys.exit(0)
    except Exception as e:
        print(f"[ERROR] Setup failed: {e}", file=sys.stderr)
        sys.exit(1)