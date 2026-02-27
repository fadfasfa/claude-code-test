import json
import sys
import os
import hmac
import hashlib
import secrets

CONTRACT = ".ai_workflow/current_contract.json"
SECRET_FILE = ".ai_workflow/.secret_key"

def get_or_create_secret() -> str:
    # 自动生成高熵密钥并进行本地隐匿
    if not os.path.exists(SECRET_FILE):
        key = secrets.token_hex(32)
        with open(SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        # 自动加入 Git 忽略名单，防止泄露
        if os.path.exists(".gitignore"):
            with open(".gitignore", "r", encoding="utf-8") as f:
                content = f.read()
            if ".secret_key" not in content:
                with open(".gitignore", "a", encoding="utf-8") as f:
                    f.write("\n.ai_workflow/.secret_key\n")

    with open(SECRET_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()

def toggle(target: str = None):
    secret = get_or_create_secret()
    data = {}

    if os.path.exists(CONTRACT):
        try:
            # 优先尝试以 utf-8 读取
            with open(CONTRACT, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (UnicodeError, json.JSONDecodeError):
            # 兼容读取旧版 utf-16 契约
            try:
                with open(CONTRACT, "r", encoding="utf-16") as f:
                    data = json.load(f)
            except Exception:
                pass

    if "task_input_from_claude" not in data:
        data["task_input_from_claude"] = {}

    current = data["task_input_from_claude"].get("executor_node", "QWEN_API")

    if target:
        new_node = "CLAUDE_API" if "claude" in target.lower() else "QWEN_API"
    else:
        new_node = "QWEN_API" if current == "CLAUDE_API" else "CLAUDE_API"

    data["task_input_from_claude"]["executor_node"] = new_node

    # 生成密码学签名 (HMAC-SHA256)
    if "signature" in data:
        del data["signature"] # 剔除旧签名以计算新哈希

    # 规范化 JSON 序列化保证哈希一致性
    payload_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
    signature = hmac.new(secret.encode('utf-8'), payload_str.encode('utf-8'), hashlib.sha256).hexdigest()
    data["signature"] = signature

    # 强制以纯血 utf-8 写入
    with open(CONTRACT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    status = "切换到" if current != new_node else "已是"
    print(f"[OK] 执行者身份：{current} -> {new_node} (已签发 HMAC 令牌)")

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    toggle(target)
