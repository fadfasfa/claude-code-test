#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
设置真正的 Git pre-commit 钩子，强制进行 AST 安全扫描。
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

    # Bash 钩子脚本内容
    hook_script = '''#!/bin/bash
# Git pre-commit hook for AST security scanning

# 获取所有暂存的 Python 文件
staged_files=$(git diff --cached --name-only | grep '\\.py$')

# 如果没有暂存的 Python 文件，直接通过
if [ -z "$staged_files" ]; then
    exit 0
fi

# 对每个暂存文件进行 AST 扫描
failed=0
while IFS= read -r file; do
    # 跳过已删除的文件
    if [ ! -f "$file" ]; then
        continue
    fi

    # 执行 AST 安全扫描
    python -c "
import sys
sys.path.insert(0, '.ai_workflow')
from pre_execution_check import check_dangerous_calls

file_path = r'$file'.replace(chr(92), '/')
try:
    with open(file_path, 'r', encoding='utf-8') as f:
        code = f.read()
    # Apply exemption rules for infrastructure files
    exempted_modules = []
    if file_path.startswith('.ai_workflow/') or file_path.startswith('scripts/'):
        exempted_modules = ['subprocess', 'os']
    safe, violations = check_dangerous_calls(code, exempted_modules=exempted_modules)
    if not safe:
        print(f'[HOOK-INTERCEPT] Git Hook AST intercept [{file_path}]:')
        for violation in violations:
            print(f'   - {violation}')
        sys.exit(1)
except Exception as e:
    print(f'[ERROR] Error scanning {file_path}: {e}')
    sys.exit(1)
" "$file"

    if [ $? -ne 0 ]; then
        failed=1
    fi
done <<< "$staged_files"

if [ $failed -eq 1 ]; then
    echo "[ERROR] Git pre-commit hook check failed, commit aborted"
    exit 1
fi

echo "[SUCCESS] AST security scan passed"
exit 0
'''

    # 写入钩子脚本，使用 \n 作为换行符
    with open(hook_path, 'w', newline='\n', encoding='utf-8') as f:
        f.write(hook_script)

    # 赋予执行权限
    os.chmod(hook_path, 0o755)

    print(f"[SUCCESS] Git pre-commit hook created: {hook_path}")
    print(f"[SUCCESS] Hook permissions set to: 0o755")
    print("\n[SUCCESS] Git hook setup completed!")
    print("  All subsequent git commit operations will go through AST security scan")

if __name__ == '__main__':
    try:
        setup_git_hook()
        sys.exit(0)
    except Exception as e:
        print(f"[ERROR] Setup failed: {e}", file=sys.stderr)
        sys.exit(1)
