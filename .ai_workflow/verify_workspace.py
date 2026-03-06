import os
import sys
import subprocess
import json
import hmac
import hashlib
import keyring  # [NEW] V4.0 Credential Dependency

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# V4.0 Precise Revert Logic
def create_workspace_snapshot():
    """在执行扫描前捕获工作区物理状态"""
    snapshot_path = ".ai_workflow/pre_merge_snapshot.txt"
    # 使用 git status --porcelain 获取最纯净的变更列表
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    with open(snapshot_path, "w", encoding="utf-8") as f:
        f.write(result.stdout)

def get_ai_change_list():
    """对比快照，识别 AI 产生的实际增量文件"""
    snapshot_path = ".ai_workflow/pre_merge_snapshot.txt"
    if not os.path.exists(snapshot_path):
        return []

    with open(snapshot_path, "r", encoding="utf-8") as f:
        old_status = set(f.readlines())

    current_status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    ).stdout.splitlines()
    # 差集逻辑：仅回滚快照中不存在或状态已变更的文件
    changes = [line[3:] for line in current_status if line + "\n" not in old_status]
    return changes

def verify_contract_v4():
    """基于 Windows Credential Manager 的 HMAC 校验"""
    # 1. 从 keyring 获取密钥 (由人类通过脚本预存)
    secret = keyring.get_password("AI_WORKFLOW", "ADMIN_SECRET")
    if not secret:
        print("[ERR_AUTH_FAILED] System credential 'AI_WORKFLOW' not found.")
        return False

    contract_path = "current_contract.json"
    if not os.path.exists(contract_path):
        return False

    with open(contract_path, "r", encoding="utf-8") as f:
        contract = json.load(f)

    # 2. 校验范围扩大：复合 agents.md + whitelist.json
    with open("agents.md", "r", encoding="utf-8") as f:
        a_content = f.read()
    with open(".ai_workflow/whitelist.json", "r", encoding="utf-8") as f:
        w_content = f.read()

    payload = a_content + w_content + json.dumps(contract["task_input_from_claude"], sort_keys=True)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    return hmac.compare_digest(signature, contract.get("signature", ""))

STASH_MARKER_PATH = ".ai_workflow/STASH_CREATED"

def check_and_stash():
    """V4.4: 幂等写入 STASH_CREATED 标记，防止重复 stash"""
    # 幂等性保障：若标记已为 true，跳过重复 stash
    if os.path.exists(STASH_MARKER_PATH):
        with open(STASH_MARKER_PATH, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing == "true":
            print("[STASH] Marker already set to true, skipping duplicate stash.")
            return True

    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    has_unstaged = bool(result.stdout.strip())
    with open(STASH_MARKER_PATH, "w", encoding="utf-8") as f:
        f.write("true" if has_unstaged else "false")
    return has_unstaged

def read_stash_marker():
    """读取 STASH_CREATED 标记文件"""
    if not os.path.exists(STASH_MARKER_PATH):
        return False
    with open(STASH_MARKER_PATH, "r", encoding="utf-8") as f:
        return f.read().strip() == "true"

def main():
    # V4.4 入口：先创建快照，写入暂存标记，再执行鉴权
    create_workspace_snapshot()
    check_and_stash()

    if not verify_contract_v4():
        print("[BLOCKED] V4.0 contract verification failed.")
        ai_changes = get_ai_change_list()

        # V4.4 强化回滚：鉴权失败时，必须先还原用户现场 (stash pop)，再抹除 AI 改动
        stash_was_created = read_stash_marker()

        if stash_was_created:
            print("[REVERT] 检测到 STASH_CREATED=true，先执行 git stash pop 还原用户现场...")
            pop_result = subprocess.run(["git", "stash", "pop"], capture_output=True, text=True)
            if pop_result.returncode != 0:
                print(f"[WARNING] git stash pop failed: {pop_result.stderr.strip()}")

        if ai_changes:
            print(f"[REVERT] Rolling back {len(ai_changes)} AI-generated changes:")
            for f in ai_changes:
                print(f"  - {f}")
            # V4.4 精确回滚：先解除暂存区，再还原工作区
            subprocess.run(["git", "restore", "--staged", "--"] + ai_changes, capture_output=True)
            subprocess.run(["git", "checkout", "--"] + ai_changes, capture_output=True)

        sys.exit(1)

    print("[SUCCESS] V4.4 verification passed.")
    sys.exit(0)

if __name__ == "__main__":
    main()
