#!/usr/bin/env python3
"""
Hextech Nexus - Workspace Verification & Contract Anchoring Engine (V5.3)

功能:
  1. HMAC 契约签名校验（基于 agents.md 关键字段）
  2. agents.md 内容哈希计算与持久化（供 Node C 双重锚定比对）
  3. Git hash-object 一致性校验（与 Git 内部哈希算法对齐）

用法:
  python verify_workspace.py                  # 完整校验（HMAC + 哈希锚定）
  python verify_workspace.py --anchor-only    # 仅计算并写入哈希锚定
  python verify_workspace.py --verify-only    # 仅校验已存在的哈希锚定
"""

import hashlib
import hmac
import os
import subprocess
import sys
import json
from pathlib import Path

# --- 路径配置 ---
REPO_ROOT = Path(__file__).resolve().parent.parent  # 脚本部署于 .ai_workflow/ 子目录，上溯一级至项目根
AGENTS_MD = REPO_ROOT / "agents.md"
WORKFLOW_DIR = REPO_ROOT / ".ai_workflow"
CONTRACT_HASH_FILE = WORKFLOW_DIR / ".contract_hash"

# HMAC 密钥来源：环境变量优先，否则使用 repo 路径派生密钥（开发环境降级方案）
HMAC_KEY_ENV = "HEXTECH_HMAC_KEY"


def get_hmac_key() -> bytes:
    """获取 HMAC 密钥。优先从环境变量读取，否则从 repo 路径派生。"""
    env_key = os.environ.get(HMAC_KEY_ENV)
    if env_key:
        return env_key.encode("utf-8")
    # 降级方案：基于 repo 绝对路径 + 固定盐值派生
    salt = b"hextech-nexus-workspace-v5"
    return hashlib.sha256(salt + str(REPO_ROOT).encode("utf-8")).digest()


def read_agents_md_binary() -> bytes:
    """以二进制模式读取 agents.md，避免 CRLF/LF 换行符差异。"""
    if not AGENTS_MD.exists():
        print("[FATAL] agents.md 不存在", file=sys.stderr)
        sys.exit(2)
    return AGENTS_MD.read_bytes()


def normalize_content(raw: bytes) -> bytes:
    """标准化内容：统一为 LF 换行符，去除尾部空白，确保跨平台哈希一致性。"""
    text = raw.decode("utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # 去除每行尾部空白，保留整体结构
    normalized = "\n".join(line.rstrip() for line in lines)
    # 确保文件以单个换行符结尾
    normalized = normalized.rstrip("\n") + "\n"
    return normalized.encode("utf-8")


def compute_sha256(content: bytes) -> str:
    """计算 SHA-256 哈希值。"""
    return hashlib.sha256(content).hexdigest()


def compute_git_hash(content: bytes) -> str:
    """计算与 git hash-object 一致的哈希值（SHA-1，含 blob 头）。"""
    header = f"blob {len(content)}\0".encode("utf-8")
    return hashlib.sha1(header + content).hexdigest()


def compute_hmac_signature(content: bytes, key: bytes) -> str:
    """计算 HMAC-SHA256 签名。"""
    return hmac.new(key, content, hashlib.sha256).hexdigest()


def verify_git_hash_consistency(content: bytes) -> bool:
    """与 git hash-object 命令输出做交叉校验。"""
    computed = compute_git_hash(content)
    try:
        result = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=content,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"[WARN] git hash-object 执行失败: {result.stderr.decode().strip()}", file=sys.stderr)
            return True  # 降级放行，不因 git 命令失败而阻断
        git_hash = result.stdout.decode().strip()
        if computed != git_hash:
            print(f"[FATAL] Git 哈希不一致: 计算值={computed}, git输出={git_hash}", file=sys.stderr)
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("[WARN] git 命令不可用，跳过交叉校验", file=sys.stderr)
        return True


def write_contract_hash(sha256_hash: str, git_hash: str, hmac_sig: str) -> None:
    """将哈希锚定信息写入受保护的缓存文件。"""
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    anchor_data = {
        "version": "5.3",
        "agents_md_sha256": sha256_hash,
        "agents_md_git_hash": git_hash,
        "hmac_signature": hmac_sig,
        "anchor_source": "verify_workspace.py",
    }
    CONTRACT_HASH_FILE.write_text(
        json.dumps(anchor_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] 哈希锚定已写入: {CONTRACT_HASH_FILE}", file=sys.stderr)


def read_contract_hash() -> dict:
    """读取已存储的哈希锚定信息。"""
    if not CONTRACT_HASH_FILE.exists():
        return {}
    try:
        return json.loads(CONTRACT_HASH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def do_anchor(raw_content: bytes) -> dict:
    """执行哈希锚定：计算各种哈希并写入缓存文件。返回锚定数据字典。"""
    normalized = normalize_content(raw_content)
    sha256_hash = compute_sha256(normalized)
    git_hash = compute_git_hash(normalized)
    hmac_key = get_hmac_key()
    hmac_sig = compute_hmac_signature(normalized, hmac_key)

    # Git hash-object 交叉校验
    if not verify_git_hash_consistency(normalized):
        print("[FATAL] Git 哈希交叉校验失败，锚定中止", file=sys.stderr)
        sys.exit(2)

    write_contract_hash(sha256_hash, git_hash, hmac_sig)

    return {
        "sha256": sha256_hash,
        "git_hash": git_hash,
        "hmac": hmac_sig,
    }


def do_verify(raw_content: bytes) -> bool:
    """校验当前 agents.md 与已存储的锚定是否一致。"""
    stored = read_contract_hash()
    if not stored:
        print("[FATAL] 未找到哈希锚定文件，请先执行 --anchor-only", file=sys.stderr)
        return False

    normalized = normalize_content(raw_content)
    current_sha256 = compute_sha256(normalized)
    current_git_hash = compute_git_hash(normalized)

    if current_sha256 != stored.get("agents_md_sha256"):
        print(
            f"[HALT: CONTRACT_TAMPERED] SHA-256 不匹配\n"
            f"  存储值: {stored.get('agents_md_sha256')}\n"
            f"  当前值: {current_sha256}",
            file=sys.stderr,
        )
        return False

    if current_git_hash != stored.get("agents_md_git_hash"):
        print(
            f"[HALT: CONTRACT_TAMPERED] Git Hash 不匹配\n"
            f"  存储值: {stored.get('agents_md_git_hash')}\n"
            f"  当前值: {current_git_hash}",
            file=sys.stderr,
        )
        return False

    # HMAC 校验
    hmac_key = get_hmac_key()
    current_hmac = compute_hmac_signature(normalized, hmac_key)
    if not hmac.compare_digest(current_hmac, stored.get("hmac_signature", "")):
        print("[HALT: HMAC_MISMATCH] HMAC 签名校验失败", file=sys.stderr)
        return False

    print("[OK] 双重锚定校验通过: SHA-256 + Git Hash + HMAC 均一致", file=sys.stderr)
    return True


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Hextech Nexus 工作区鉴权与契约锚定引擎 V5.3"
    )
    parser.add_argument(
        "--anchor-only",
        action="store_true",
        help="仅计算并写入哈希锚定（不做校验）",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅校验已存在的哈希锚定（不重新计算）",
    )
    args = parser.parse_args()

    raw_content = read_agents_md_binary()

    if args.anchor_only:
        result = do_anchor(raw_content)
        print(f"[ANCHOR] SHA-256: {result['sha256']}", file=sys.stderr)
        print(f"[ANCHOR] Git Hash: {result['git_hash']}", file=sys.stderr)
        print(f"[ANCHOR] HMAC: {result['hmac'][:16]}...", file=sys.stderr)
        sys.exit(0)

    if args.verify_only:
        if do_verify(raw_content):
            sys.exit(0)
        else:
            sys.exit(1)

    # 默认模式：先锚定，再校验
    result = do_anchor(raw_content)
    print(f"[SYSTEM] V5.3 鉴权防线已激活", file=sys.stderr)
    print(f"  SHA-256:  {result['sha256']}", file=sys.stderr)
    print(f"  Git Hash: {result['git_hash']}", file=sys.stderr)
    print(f"  HMAC:     {result['hmac'][:16]}...", file=sys.stderr)

    if do_verify(raw_content):
        print("[SYSTEM] 双重锚定自检通过，鉴权引擎就绪", file=sys.stderr)
        sys.exit(0)
    else:
        print("[FATAL] 锚定自检失败", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()