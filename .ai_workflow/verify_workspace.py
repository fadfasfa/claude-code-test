import os, sys, subprocess, json, shutil, fnmatch, re
from typing import Dict, List, Set, Tuple
from datetime import datetime, timedelta

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

def get_claude_auth_paths(contract_path: str) -> Set[str]:
    """读取最高控制权契约，返回当前授权的路径集合"""
    try:
        if not os.path.exists(contract_path): return set()
        with open(contract_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            sudo_config = data.get("claude_sudo", {})
            if sudo_config.get("session_active") is True:
                return set(sudo_config.get("authorized_paths", []))
    except Exception:
        pass
    return set()

def categorize_changes(changes: List[Dict], whitelist: Dict[str, str], auth_paths: Set[str]) -> Tuple:
    unauthorized = {}
    pending_ast = []

    def match_level(path: str):
        # 【核心拦截】如果路径在 Claude Sudo 授权名单中，直接赋予最高通行证
        if path in auth_paths:
            return "LOOSE"
            
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

def execute_atomic_revert(unauthorized: Dict, base_commit: str) -> bool:
    if not unauthorized: return False

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, timestamp)
    os.makedirs(backup_path, exist_ok=True)

    # 1. 生成 Patch 文件
    patch_file = os.path.join(backup_path, "changes.patch")
    with open(patch_file, "w", encoding="utf-8") as f:
        subprocess.run(["git", "diff", "HEAD", "--"] + list(unauthorized.keys()), stdout=f, text=True)

    # 2. 物理拷贝原始文件
    for file_path in unauthorized.keys():
        if os.path.exists(file_path):
            dest_path = os.path.join(backup_path, file_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copy2(file_path, dest_path)

    # 3. 生成留存追溯凭证 manifest.json
    manifest = {
        "timestamp": timestamp,
        "base_commit": base_commit,
        "unauthorized_changes": unauthorized
    }
    manifest_file = os.path.join(backup_path, "manifest.json")
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    # 4. 执行原有回滚逻辑
    stash_msg = f"ai_workflow_snapshot_{timestamp}"
    subprocess.run(["git", "stash", "push", "-u", "-m", stash_msg, "--"] + list(unauthorized.keys()), capture_output=True)

    for file_path in unauthorized.keys():
        subprocess.run(["git", "checkout", "HEAD", "--", file_path], capture_output=True)

    print(f"✅ 已执行安全回滚，案发现场快照已保存至：{backup_path}")
    return True

def cleanup_stale_backups(backup_dir=".ai_workflow/backup", max_days=7, max_keep=30):
    """静默清理过期快照：严格按正则匹配，双重淘汰（>7 天 或 超出 30 个），忽略占用报错"""
    if not os.path.exists(backup_dir):
        return

    valid_snapshots = []
    pattern = re.compile(r"^\d{8}_\d{6}$")
    now = datetime.now()

    # 1. 严格筛选合法的快照目录
    for item in os.listdir(backup_dir):
        if pattern.match(item):
            item_path = os.path.join(backup_dir, item)
            if os.path.isdir(item_path):
                try:
                    folder_time = datetime.strptime(item, "%Y%m%d_%H%M%S")
                    valid_snapshots.append((item_path, folder_time))
                except ValueError:
                    continue

    # 按时间从新到老排序
    valid_snapshots.sort(key=lambda x: x[1], reverse=True)

    to_delete = []
    kept_count = 0

    # 2. 状态机筛选淘汰名单
    for path, f_time in valid_snapshots:
        # 规则 1: 超过 7 天直接淘汰
        if now - f_time > timedelta(days=max_days):
            to_delete.append(path)
        else:
            # 规则 2: 没超 7 天，但名额已满 30 个，淘汰
            if kept_count >= max_keep:
                to_delete.append(path)
            else:
                kept_count += 1

    # 3. 机械执行删除，静默跳过占用
    for path in to_delete:
        try:
            shutil.rmtree(path)
        except Exception:
            pass  # 发生 PermissionError 或其他占用报错时直接静默跳过

def main():
    whitelist = load_whitelist(".ai_workflow/whitelist.txt")
    auth_paths = get_claude_auth_paths(".ai_workflow/current_contract.json")
    changes, base_commit = get_comprehensive_changes()
    
    if not changes:
        print("✅ 工作区无变动")
        sys.exit(0)

    unauthorized, pending_ast = categorize_changes(changes, whitelist, auth_paths)

    # ----------------- 【新增：双重审查网关】 -----------------
    if pending_ast:
        print(f"🔍 触发深度审查，正在扫描 {len(pending_ast)} 个文件...")
        # 临时将当前目录加入环境变量，以便导入 pre_execution_check
        if ".ai_workflow" not in sys.path:
            sys.path.insert(0, ".ai_workflow")
        from pre_execution_check import check_dangerous_calls

        for ast_file in pending_ast:
            if not os.path.exists(ast_file):
                continue
            
            # 防线一：静态恶意指令扫描
            with open(ast_file, "r", encoding="utf-8") as f:
                code_content = f.read()
            is_safe, violations = check_dangerous_calls(code_content)
            if not is_safe:
                print(f"🚫 [拦截] {ast_file} 触发高危动作：{violations}")
                unauthorized[ast_file] = "DANGEROUS_CODE"
                continue # 命中第一防线，直接拉黑并跳过第二防线

            # 防线二：结构退化审查 (AST 比对)
            # 使用 sys.executable 确保调用同一虚拟环境的 Python
            diff_cmd = [sys.executable, ".ai_workflow/post_check_diff.py", ast_file, "--ref", base_commit]
            result = subprocess.run(diff_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                # 若脚本退出码不为 0，视为结构遭破坏
                print(f"🚫 [拦截] {ast_file} 发生 AST 结构退化/异常:\n{result.stdout.strip()}")
                unauthorized[ast_file] = "AST_DEGRADED"
    # --------------------------------------------------------

    if unauthorized:
        print("❌ 发现未授权或未通过审查的修改，执行回滚...")
        for k in unauthorized: 
            print(f" - {k} [阻断原因：{unauthorized[k]}]")
        execute_atomic_revert(unauthorized, base_commit)
        cleanup_stale_backups()
        sys.exit(1)

    print("✅ 验证通过")
    sys.exit(0)

if __name__ == "__main__":
    main()