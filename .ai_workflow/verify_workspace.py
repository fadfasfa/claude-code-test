#!/usr/bin/env python3
import os, sys, subprocess, json, shutil, fnmatch
from typing import Dict, List, Set, Tuple
from datetime import datetime

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DEFAULT_IGNORE_PATTERNS = ['__pycache__', '.pyc', '.pyo', '.eggs', '*.egg-info', '.pytest_cache', '.claude/', 'claude/']
BACKUP_DIR = ".ai_workflow/backup"

def get_current_commit_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip()

def is_ignored_file(file_path: str) -> bool:
    basename = os.path.basename(file_path)
    for pattern in DEFAULT_IGNORE_PATTERNS:
        if pattern in file_path or basename == pattern or basename.endswith(pattern.lstrip('*')): return True
    return False

def get_comprehensive_changes() -> Tuple[List[Dict], str]:
    base_commit = get_current_commit_sha()
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    changes = []
    seen = set()
    tokens = result.stdout.split('\x00')
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        xy = token[:2]
        path = token[3:].replace("\\", "/")
        x, y = xy[0], xy[1]
        if is_ignored_file(path):
            i += 1
            continue
        # Rename: next token is old path
        if x == 'R' or y == 'R':
            old_path = tokens[i + 1].replace("\\", "/") if i + 1 < len(tokens) else None
            changes.append({"path": path, "change_type": "R", "old_path": old_path, "stage": "working"})
            seen.add(path)
            i += 2
            continue
        # Map XY to change_type: prefer staged (X), fall back to working (Y)
        raw = x if x not in (' ', '?') else y
        change_type = raw if raw not in ('?',) else 'A'
        if path not in seen:
            changes.append({"path": path, "change_type": change_type, "stage": "working"})
            seen.add(path)
        i += 1
    return changes, base_commit

def load_whitelist(filepath: str) -> Dict[str, str]:
    if not os.path.exists(filepath):
        return {}
    wl = {}
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            pattern = parts[0].replace("\\", "/")
            level = parts[1].strip("[]") if len(parts) > 1 else "STANDARD"
            wl[pattern] = level
    return wl

def categorize_changes(changes: List[Dict], whitelist: Dict[str, str]) -> Tuple:
    unauthorized = {}
    pending_ast = []

    def match_level(path: str):
        for pattern, level in whitelist.items():
            if fnmatch.fnmatch(path, pattern) or path == pattern:
                return level
        return None

    for c in changes:
        path = c["path"]
        ct = c.get("change_type", "M")
        level = match_level(path)

        if level is None:
            unauthorized[path] = ct
        elif level == "LOOSE":
            pass  # 全放行，不做 AST 检查
        elif level == "STRICT":
            if ct != "M":
                unauthorized[path] = ct
            elif path.endswith(".py"):
                pending_ast.append(path)
        else:  # STANDARD
            if ct == "D":
                pass  # 允许删除，不做 AST
            elif path.endswith(".py"):
                pending_ast.append(path)

    return unauthorized, pending_ast

def execute_atomic_revert(unauthorized: Dict) -> bool:
    if not unauthorized: return False
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stash_msg = f"ai_workflow_snapshot_{timestamp}"
    subprocess.run(["git", "stash", "push", "-u", "-m", stash_msg, "--"] + list(unauthorized.keys()), capture_output=True)

    for file_path in unauthorized.keys():
        subprocess.run(["git", "checkout", "HEAD", "--", file_path], capture_output=True)
    print("✅ 已执行安全回滚")
    return True

def main():
    whitelist = load_whitelist(".ai_workflow/whitelist.txt")
    changes, base_commit = get_comprehensive_changes()
    if not changes:
        print("✅ 工作区无变动")
        sys.exit(0)

    unauthorized, pending_ast = categorize_changes(changes, whitelist)
    if unauthorized:
        print("❌ 发现未授权修改，执行回滚...")
        for k in unauthorized: print(f" - {k}")
        execute_atomic_revert(unauthorized)
        sys.exit(1)

    print("✅ 验证通过")
    sys.exit(0)

if __name__ == "__main__":
    main()
