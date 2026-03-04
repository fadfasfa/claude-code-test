import os
import sys
import subprocess

# [Win-Encoding-Safe] Force UTF-8 for Windows compatibility
if sys.platform == 'win32':
    try:
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
    os.environ['PYTHONIOENCODING'] = 'utf-8'

def dispatch_to_reviewer():
    """
    Contract 1: Dispatcher Core Awakening Loop
    - Fetch changed file list (pure ASCII command)
    - Iterate and trigger (decoupled strategy)
    - Block-style subprocess call to reviewer
    """

    # 1. Get changed files list
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=AM"],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace'
    )

    changed_files = result.stdout.strip().split('\n')
    changed_files = [f for f in changed_files if f]  # Remove empty strings

    if not changed_files:
        print("[INFO] No staged changes detected, skipping audit.")
        return

    # 2. Filter Python files only
    python_files = [f for f in changed_files if f.endswith('.py')]

    if not python_files:
        print("[INFO] No Python files staged, skipping audit.")
        return

    print(f"[AUDIT] Detected {len(python_files)} Python file(s) to review.")

    # 3. Iterate and trigger reviewer for each file
    for file_path in python_files:
        print(f"[DISPATCH] Awakening AST Reviewer for: {file_path}")

        # Block-style subprocess call
        exit_code = subprocess.call(
            ["python", "scripts/gemini_reviewer.py", file_path],
            env=os.environ.copy()
        )

        if exit_code != 0:
            print(f"[FATAL] Audit blocked for {file_path} due to AST review failure.")
            sys.exit(1)

    print("[SUCCESS] All Python files passed AST review.")

if __name__ == "__main__":
    dispatch_to_reviewer()
