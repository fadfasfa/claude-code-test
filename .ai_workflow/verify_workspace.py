import os, sys, subprocess, json, shutil, fnmatch, re
import hmac, hashlib, copy
from typing import Dict, List, Set, Tuple
from datetime import datetime, timedelta

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DEFAULT_IGNORE_PATTERNS = ['__pycache__', '.pyc', '.pyo', '.eggs', '*.egg-info', '.pytest_cache', '.claude/', 'claude/', 'backup/', '.verify.lock']
BACKUP_DIR = ".ai_workflow/backup"
LOCK_FILE = ".ai_workflow/.verify.lock"
IMMUTABLE_CORE = {
    ".ai_workflow/current_contract.json",
    ".ai_workflow/whitelist.txt",
}
QWEN_INFRA_PATTERN = re.compile(r'^\.ai_workflow/')

def acquire_lock() -> bool:
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False

def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

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
        if x == 'R' or y == 'R':
            old_path = tokens[i + 1].replace("\\", "/") if i + 1 < len(tokens) else None
            changes.append({"path": path, "change_type": "R", "old_path": old_path, "stage": "working"})
            seen.add(path)
            i += 2
            continue
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
    # 【乱码免疫装甲】增加 errors="replace" 防止遭遇非 UTF-8 字符时崩溃
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            pattern = parts[0].replace("\\", "/")
            level = parts[1].strip("[]") if len(parts) > 1 else "STANDARD"
            wl[pattern] = level
    return wl

def verify_contract_identity(contract_data: dict) -> str:
    """【高阶密码学闭环】基于 HMAC-SHA256 的零信任鉴权"""
    declared_node = contract_data.get("task_input_from_claude", {}).get("executor_node", "QWEN_API")

    # 如果主动声明为低权限，直接放行（无需耗费算力验签）
    if declared_node == "QWEN_API":
        return "QWEN_API"

    secret_file = ".ai_workflow/.secret_key"
    if not os.path.exists(secret_file):
        print("\n[WARNING] Secret key file missing, refusing privilege escalation! Forced downgrade to QWEN_API.\n")
        return "QWEN_API"

    try:
        with open(secret_file, "r", encoding="utf-8") as f:
            secret = f.read().strip()

        provided_signature = contract_data.get("signature")
        if not provided_signature:
            print("\n[WARNING] Contract missing HMAC signature, suspected forgery! Forced downgrade to QWEN_API.\n")
            return "QWEN_API"

        # 剥离签名本身以重构原始摘要载荷
        data_to_verify = copy.deepcopy(contract_data)
        del data_to_verify["signature"]

        payload_str = json.dumps(data_to_verify, sort_keys=True, separators=(',', ':'))
        expected_signature = hmac.new(secret.encode('utf-8'), payload_str.encode('utf-8'), hashlib.sha256).hexdigest()

        # 防止时序攻击的恒定时间比较，同时清理签名中的隐藏空白字符
        if hmac.compare_digest(expected_signature.strip(), provided_signature.strip()):
            return declared_node
        else:
            if contract_data.get("__from_git_history"):
                print("\n[WARNING] Git history contract detected, signature invalid, auto-downgrade to QWEN_API per zero-trust principle\n")
                return "QWEN_API"
            print("\n[WARNING] Contract HMAC signature verification failed, file tampered! Forced downgrade to QWEN_API.\n")
            return "QWEN_API"

    except Exception as e:
        if contract_data.get("__from_git_history"):
            print("\n[WARNING] Git history contract detected, signature invalid, auto-downgrade to QWEN_API per zero-trust principle\n")
            return "QWEN_API"
        print(f"\n[WARNING] Signature verification environment error: {e}. Forced downgrade to QWEN_API.\n")
        return "QWEN_API"

def get_claude_auth_from_git() -> dict:
    contract_path = ".ai_workflow/current_contract.json"
    if os.path.exists(contract_path):
        try:
            with open(contract_path, "rb") as f:
                raw = f.read()
            try:
                text = raw.decode('utf-8').strip()
            except UnicodeDecodeError:
                try:
                    text = raw.decode('utf-16').strip()
                    if text.startswith('\ufeff'):
                        text = text[1:]
                except UnicodeDecodeError:
                    text = raw.decode('gbk', errors="replace")
            return json.loads(text)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass

    result = subprocess.run(
        ["git", "show", "HEAD:.ai_workflow/current_contract.json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        return {}
    try:
        contract = json.loads(result.stdout)
        contract["__from_git_history"] = True
        return contract
    except json.JSONDecodeError:
        return {}



def categorize_changes(changes: List[Dict], whitelist: Dict[str, str], auth_paths: Set[str], executor_node: str = "CLAUDE_API") -> Tuple:
    unauthorized = {}
    pending_ast = []

    def match_level(path: str):
        if path in auth_paths:
            return "LOOSE"
        for pattern, level in whitelist.items():
            if fnmatch.fnmatch(path, pattern) or path == pattern:
                return level
        return None

    for c in changes:
        path = c["path"]
        ct = c.get("change_type", "M")

        if path in IMMUTABLE_CORE:
            if executor_node != "CLAUDE_API" or path not in auth_paths:
                unauthorized[path] = "IMMUTABLE_CORE_VIOLATION"
                continue
            level = "LOOSE"
        else:
            if executor_node == "QWEN_API" and QWEN_INFRA_PATTERN.match(path):
                unauthorized[path] = "QWEN_INFRA_FORBIDDEN"
                continue
            level = match_level(path)

        if level is None:
            unauthorized[path] = ct
        elif level == "LOOSE":
            pass
        elif level == "STRICT":
            if ct != "M":
                unauthorized[path] = ct
            elif path.endswith(".py"):
                pending_ast.append(path)
        else:
            if ct == "D":
                pass
            elif path.endswith(".py"):
                pending_ast.append(path)

    return unauthorized, pending_ast

def execute_atomic_revert(unauthorized: Dict, base_commit: str) -> bool:
    if not unauthorized: return False
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, timestamp)
    os.makedirs(backup_path, exist_ok=True)

    patch_file = os.path.join(backup_path, "changes.patch")
    with open(patch_file, "w", encoding="utf-8") as f:
        subprocess.run(["git", "diff", "HEAD", "--"] + list(unauthorized.keys()), stdout=f, text=True)

    for file_path in unauthorized.keys():
        if os.path.exists(file_path):
            if os.path.isfile(file_path):
                dest_path = os.path.join(backup_path, file_path)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(file_path, dest_path)

    manifest = {
        "timestamp": timestamp,
        "base_commit": base_commit,
        "unauthorized_changes": unauthorized
    }
    manifest_file = os.path.join(backup_path, "manifest.json")
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    stash_msg = f"ai_workflow_snapshot_{timestamp}"
    subprocess.run(["git", "stash", "push", "-u", "-m", stash_msg, "--"] + list(unauthorized.keys()), capture_output=True)

    for file_path in unauthorized.keys():
        subprocess.run(["git", "checkout", "HEAD", "--", file_path], capture_output=True)

    print(f"[SUCCESS] Security rollback executed, incident snapshot saved to: {backup_path}")
    return True

def cleanup_stale_backups(backup_dir=".ai_workflow/backup", max_days=7, max_keep=30):
    if not os.path.exists(backup_dir):
        return
    valid_snapshots = []
    pattern = re.compile(r"^\d{8}_\d{6}$")
    now = datetime.now()

    for item in os.listdir(backup_dir):
        if pattern.match(item):
            item_path = os.path.join(backup_dir, item)
            if os.path.isdir(item_path):
                try:
                    folder_time = datetime.strptime(item, "%Y%m%d_%H%M%S")
                    valid_snapshots.append((item_path, folder_time))
                except ValueError:
                    continue

    valid_snapshots.sort(key=lambda x: x[1], reverse=True)
    to_delete = []
    kept_count = 0

    for path, f_time in valid_snapshots:
        if now - f_time > timedelta(days=max_days):
            to_delete.append(path)
        else:
            if kept_count >= max_keep:
                to_delete.append(path)
            else:
                kept_count += 1

    for path in to_delete:
        try:
            shutil.rmtree(path)
        except Exception:
            pass

def main():
    if not acquire_lock():
        sys.exit(0)

    try:
        contract = get_claude_auth_from_git()
        auth_paths = set(contract.get("claude_sudo", {}).get("authorized_paths", []))

        # 【高阶密码学闭环：HMAC 验签取代物理探针】
        executor_node = verify_contract_identity(contract)

        whitelist = load_whitelist(".ai_workflow/whitelist.txt")
        changes, base_commit = get_comprehensive_changes()

        if not changes:
            print("[SUCCESS] No changes in workspace")
            return

        unauthorized, pending_ast = categorize_changes(changes, whitelist, auth_paths, executor_node)

        if executor_node != "QWEN_API":
            pending_ast.clear()
            unauthorized.clear()

        if pending_ast:
            print(f"[SCAN] Starting deep review, scanning {len(pending_ast)} files...")
            if ".ai_workflow" not in sys.path:
                sys.path.insert(0, ".ai_workflow")
            from pre_execution_check import check_dangerous_calls

            for ast_file in pending_ast:
                if not os.path.exists(ast_file):
                    continue

                if executor_node != "QWEN_API":
                    continue

                git_path = ast_file.replace("\\", "/")
                old_code_result = subprocess.run(
                    ["git", "show", f"{base_commit}:{git_path}"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace"
                )
                old_code = old_code_result.stdout if old_code_result.returncode == 0 else None

                with open(ast_file, "r", encoding="utf-8") as f:
                    new_code = f.read()

                exempted_modules = []
                if executor_node == "CLAUDE_API":
                    if ast_file.startswith('.ai_workflow/') or ast_file.startswith('scripts/'):
                        exempted_modules = ['subprocess', 'os']

                is_safe, violations = check_dangerous_calls(new_code, exempted_modules=exempted_modules)
                if not is_safe:
                    if old_code is not None:
                        old_is_safe, _ = check_dangerous_calls(old_code, exempted_modules=exempted_modules)
                        if old_is_safe:
                            print(f"[INTERCEPT] {ast_file} triggered new dangerous action: {violations}")
                            unauthorized[ast_file] = "DANGEROUS_CODE"
                            continue
                    else:
                        print(f"[INTERCEPT] {ast_file} triggered new dangerous action: {violations}")
                        unauthorized[ast_file] = "DANGEROUS_CODE"
                        continue

                diff_cmd = [sys.executable, ".ai_workflow/post_check_diff.py", ast_file, "--ref", base_commit]
                creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' and hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                result = subprocess.run(diff_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creation_flags)

                if result.returncode != 0:
                    stdout_lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
                    destructive_violations = []
                    for line in stdout_lines:
                        if 'ADDED' in line:
                            continue
                        if any(keyword in line for keyword in ['REMOVED', 'CHANGED', 'BROADENED']):
                            destructive_violations.append(line)

                    if destructive_violations:
                        print(f"[INTERCEPT] {ast_file} AST structure degraded/abnormal:\n" + '\n'.join(destructive_violations))
                        unauthorized[ast_file] = "AST_DEGRADED"

        if unauthorized:
            print("[ERROR] Found unauthorized or unapproved changes, executing rollback...")
            for k in unauthorized:
                print(f" - {k} [Reason: {unauthorized[k]}]")
            execute_atomic_revert(unauthorized, base_commit)
            cleanup_stale_backups()
            sys.exit(1)

        print("[SUCCESS] Verification passed")
        sys.exit(0)
    finally:
        release_lock()

if __name__ == "__main__":
    main()
