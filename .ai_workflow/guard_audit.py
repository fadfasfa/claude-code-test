#!/usr/bin/env python3
import sys, os, subprocess
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
def main():
    if len(sys.argv) < 2:
        print('用法：python guard_audit.py <目标文件>')
        sys.exit(1)
    target = sys.argv[1]
    sys.path.insert(0, '.ai_workflow')
    from pre_execution_check import check_dangerous_calls
    with open(target, 'r', encoding='utf-8', errors='replace') as f: code = f.read()
    is_safe, violations = check_dangerous_calls(code)
    if not is_safe:
        print(f'🚫 [拦截] AST 静态扫描失败：{violations}')
        sys.exit(1)
    result = subprocess.run([sys.executable, '.ai_workflow/post_check_diff.py', target, '--ref', 'HEAD'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        print(f'🚫 [拦截] 增量防降级扫描失败：\n{result.stdout}\n{result.stderr}')
        sys.exit(1)
    subprocess.run(['git', 'add', target], check=True)
    subprocess.run(['git', 'commit', '-m', f'auto: AST audit passed for {target}'], check=True)
    print(f'🔵 审查通过，{target} 已自动 Commit。')
if __name__ == '__main__': main()
