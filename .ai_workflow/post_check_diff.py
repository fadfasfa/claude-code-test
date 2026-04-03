#!/usr/bin/env python3
#
# Post-change AST validator for Python source files.
# [V7.0 - 分级处置模式：BLOCK / DENY / WARN，敏感路径解析失败升级为 AUDIT-DENY]
#
# 变更说明（V6.0 -> V7.0）：
# - [优先级1] 引入三级处置：BLOCK（立即阻断）/ DENY（等待审计）/ WARN（仅告警）
# - [优先级2] 敏感路径命中时禁止降级处置，解析失败直接 AUDIT-DENY
# - [优先级4] 新增 SEC-002 secrets/凭据硬编码检测
# - [优先级4] 新增 SEC-003 危险调用检测（shell=True / yaml.load / pickle.loads）
# - 修复 V6.0 main() 中 --before/--after 分支的缩进语法错误
#

import ast
import json
import re
import subprocess
import sys
import argparse
import os
from typing import Dict, List, Set, Tuple, Optional

TRY_TYPES = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)

# ============================================================
# 分级处置常量（优先级1）
# ============================================================
SEV_BLOCK = "BLOCK"   # 命中后立即阻断，不等审计
SEV_DENY  = "DENY"    # 高风险，等待 Node C 审计后决定
SEV_WARN  = "WARN"    # 低风险，仅记录告警

# 各规则对应的默认严重级别
RULE_SEVERITY: Dict[str, str] = {
    "SEC-001": SEV_BLOCK,  # eval/exec/动态 getattr 注入
    "SEC-002": SEV_BLOCK,  # secrets / 凭据硬编码
    "SEC-003": SEV_DENY,   # shell=True / yaml.load / pickle.loads
    "IMPORT ADDED":    SEV_WARN,
    "FUNC REMOVED":    SEV_DENY,
    "CLASS REMOVED":   SEV_DENY,
    "SIG CHANGED":     SEV_DENY,
    "METHOD REMOVED":  SEV_DENY,
    "METHOD ADDED":    SEV_WARN,
    "TRY REMOVED":     SEV_DENY,
    "EXCEPT BROADENED":SEV_DENY,
}

def get_rule_severity(violation: str) -> str:
    for rule_tag, sev in RULE_SEVERITY.items():
        if f"[{rule_tag}]" in violation or violation.startswith(f"[{rule_tag}"):
            return sev
    return SEV_WARN

def aggregate_severity(violations: List[str]) -> Optional[str]:
    if not violations:
        return None
    sevs = [get_rule_severity(v) for v in violations]
    if SEV_BLOCK in sevs:
        return SEV_BLOCK
    if SEV_DENY in sevs:
        return SEV_DENY
    return SEV_WARN

# ============================================================
# 敏感路径注册表（优先级2）
# ============================================================
SENSITIVE_PATHS: Set[str] = {
    "lock-core.ps1",
    "unlock-core.ps1",
    ".git/hooks/post-commit",
    ".git/hooks/pre-commit",
    ".git/hooks/pre-push",
    ".ai_workflow/audit_log.txt",
    ".ai_workflow/runtime_state_cc.json",
    ".ai_workflow/runtime_state_ag.json",
    ".ai_workflow/runtime_state_cx.json",
    ".ai_workflow/post_check_diff.py",
}

def is_sensitive_path(filename: str) -> bool:
    normalized = filename.replace("\\", "/").lower()
    for sp in SENSITIVE_PATHS:
        if normalized.endswith(sp.lower()):
            return True
    return False

# ============================================================
# 输出函数
# ============================================================
def emit_pass() -> None:
    sys.exit(0)

def emit_result(violations: List[str], is_sensitive: bool = False) -> None:
    """
    V7.0 分级输出：
    - 敏感路径 + 有违规 → 强制 BLOCK
    - 普通路径 → 按规则聚合严重级别
    """
    severity = aggregate_severity(violations)
    if is_sensitive and violations:
        severity = SEV_BLOCK  # 敏感路径不允许降级

    has_sec001 = any("[SEC-001]" in v for v in violations)

    print(json.dumps({
        "status":    "WARNING" if violations else "PASSED",
        "severity":  severity,          # BLOCK / DENY / WARN / null
        "sec001_hit": has_sec001,
        "is_sensitive_path": is_sensitive,
        "violations": violations,
    }, ensure_ascii=False))
    sys.exit(1 if violations else 0)

def emit_fatal(raw_output: str, is_sensitive: bool = False) -> None:
    """
    V7.0：敏感路径解析失败 → AUDIT-DENY（不能当作"扫过了"）
    """
    status = "AUDIT-DENY" if is_sensitive else "PARSE_ERROR"
    print(json.dumps({
        "status":     status,
        "error_code": "ERR_PARSE_FAILED",
        "is_sensitive_path": is_sensitive,
        "raw_output": raw_output,
    }, ensure_ascii=False))
    sys.exit(2)

# ============================================================
# Git / 文件读取
# ============================================================
def get_source_git(target_file: str, ref: str, is_sensitive: bool = False) -> str:
    git_path = target_file.replace("\\", "/")
    result = subprocess.run(
        ["git", "show", f"{ref}:{git_path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        if "fatal: Path" in result.stderr or "exists on disk, but not in" in result.stderr:
            return ""
        emit_fatal(
            f"git show {ref}:{git_path} 失败（退出码 {result.returncode}）：{result.stderr.strip()}",
            is_sensitive=is_sensitive,
        )
    return result.stdout

def get_source_file(path: str, is_sensitive: bool = False) -> str:
    if not os.path.isfile(path):
        emit_fatal(f"文件不存在：{path}", is_sensitive=is_sensitive)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

def resolve_sources(args: argparse.Namespace, is_sensitive: bool) -> Tuple[str, str]:
    if args.before is not None:
        return (
            get_source_file(args.before, is_sensitive),
            get_source_file(args.after,  is_sensitive),
        )
    else:
        ref = args.ref or "HEAD"
        return (
            get_source_git(args.target_file, ref,       is_sensitive),
            get_source_file(args.target_file,           is_sensitive),
        )

def parse_source(source: str, label: str) -> ast.Module:
    try:
        return ast.parse(source, type_comments=False)
    except SyntaxError:
        return ast.Module(body=[], type_ignores=[])

# ============================================================
# 检测逻辑（原有）
# ============================================================
def extract_import_keys(tree: ast.Module) -> Set[str]:
    keys = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname: keys.add(f"import:{alias.name} as {alias.asname}")
                else: keys.add(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            module = prefix + (node.module or "")
            for alias in node.names:
                if alias.asname: keys.add(f"from:{module}:{alias.name} as {alias.asname}")
                else: keys.add(f"from:{module}:{alias.name}")
    return keys

def signature_of(node: ast.FunctionDef) -> str:
    args = node.args
    parts = []
    for a in args.posonlyargs: parts.append(a.arg)
    if args.posonlyargs: parts.append("/")
    for a in args.args: parts.append(a.arg)
    if args.vararg: parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs: parts.append("*")
    for a in args.kwonlyargs: parts.append(a.arg)
    if args.kwarg: parts.append("**" + args.kwarg.arg)
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    return f"{prefix}{node.name}({', '.join(parts)})"

def extract_interfaces(tree: ast.Module) -> Dict[str, dict]:
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = {
                "kind": "function", "sig": signature_of(node),
                "lineno": node.lineno, "decorators": [ast.unparse(d) for d in node.decorator_list],
            }
        elif isinstance(node, ast.ClassDef):
            methods = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    key = f"{node.name}.{item.name}"
                    methods[key] = {
                        "sig": signature_of(item), "lineno": item.lineno,
                        "decorators": [ast.unparse(d) for d in item.decorator_list],
                    }
            result[node.name] = {"kind": "class", "lineno": node.lineno, "methods": methods}
    return result

def extract_try_locations(tree: ast.Module) -> List[Tuple[int, int]]:
    locations = []
    for node in ast.walk(tree):
        if isinstance(node, TRY_TYPES): locations.append((node.lineno, node.col_offset))
    return sorted(locations)

def extract_except_signatures(tree: ast.Module) -> Dict[Tuple[int, int], List[str]]:
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, TRY_TYPES):
            sigs = []
            for h in node.handlers:
                if h.type is None: sigs.append("__bare__")
                else: sigs.append(ast.unparse(h.type))
            result[(node.lineno, node.col_offset)] = sigs
    return result

def get_changed_python_files(base_branch: str, target_branch: str) -> List[str]:
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_branch}...{target_branch}", "--name-only", "--diff-filter=AM"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            emit_fatal(f"git diff 失败：{result.stderr.strip()}")
        files = []
        for line in result.stdout.strip().split('\n'):
            if line.strip() and line.strip().endswith('.py'):
                files.append(line.strip().replace("\\", "/"))
        return files
    except Exception as e:
        emit_fatal(f"获取变更的 Python 文件失败：{str(e)}")

BROAD_EXCEPTIONS = {"Exception", "BaseException", "__bare__"}

def is_downgrade(before_types: List[str], after_types: List[str]) -> bool:
    before_set = set(before_types)
    after_set = set(after_types)
    has_specific_before = bool(before_set - BROAD_EXCEPTIONS)
    introduced_broad = bool(after_set & BROAD_EXCEPTIONS - before_set)
    return has_specific_before and introduced_broad

STDLIB_MODULES = {
    "abc", "ast", "asyncio", "base64", "builtins", "collections", "concurrent",
    "contextlib", "copy", "csv", "dataclasses", "datetime", "decimal", "difflib",
    "email", "enum", "fileinput", "fnmatch", "fractions", "ftplib", "functools",
    "gc", "getpass", "glob", "gzip", "hashlib", "heapq", "hmac", "html", "http",
    "imaplib", "importlib", "inspect", "io", "itertools", "json", "keyword",
    "linecache", "locale", "logging", "math", "mimetypes", "multiprocessing",
    "numbers", "operator", "os", "pathlib", "pickle", "platform", "pprint",
    "queue", "random", "re", "shlex", "shutil", "signal", "smtplib", "socket",
    "sqlite3", "ssl", "stat", "statistics", "string", "struct", "subprocess",
    "sys", "tempfile", "textwrap", "threading", "time", "timeit", "tkinter",
    "traceback", "types", "typing", "unittest", "urllib", "uuid", "warnings",
    "weakref", "xml", "xmlrpc", "zipfile", "zipimport", "zlib",
}

def _extract_root_module(key: str) -> Optional[str]:
    if key.startswith("from:."):
        return None
    if key.startswith("import:"):
        return key[len("import:"):].split(".")[0].split(" as ")[0]
    if key.startswith("from:"):
        return key[len("from:"):].split(":")[0].split(".")[0]
    return None

def check_imports(before_tree: ast.Module, after_tree: ast.Module) -> List[str]:
    before_keys = extract_import_keys(before_tree)
    after_keys = extract_import_keys(after_tree)
    violations = []
    for key in sorted(after_keys - before_keys):
        root = _extract_root_module(key)
        if root is None or root in STDLIB_MODULES:
            continue
        violations.append(f"[IMPORT ADDED]    {key}")
    return violations

def check_interfaces(before_tree: ast.Module, after_tree: ast.Module) -> List[str]:
    before_iface = extract_interfaces(before_tree)
    after_iface = extract_interfaces(after_tree)
    violations = []
    for name in sorted(before_iface.keys() - after_iface.keys()):
        entry = before_iface[name]
        if entry["kind"] == "function":
            violations.append(f"[FUNC REMOVED]    {entry['sig']}  (was line {entry['lineno']})")
        else:
            violations.append(f"[CLASS REMOVED]   {name}  (was line {entry['lineno']})")
    for name in sorted(before_iface.keys() & after_iface.keys()):
        b_entry = before_iface[name]
        a_entry = after_iface[name]
        if b_entry["kind"] == "function" and b_entry["sig"] != a_entry["sig"]:
            violations.append(f"[SIG CHANGED]     before: {b_entry['sig']}")
            violations.append(f"                  after:  {a_entry['sig']}")
        elif b_entry["kind"] == "class":
            b_methods = b_entry["methods"]
            a_methods = a_entry["methods"]
            for mkey in sorted(b_methods.keys() - a_methods.keys()):
                m = b_methods[mkey]
                violations.append(f"[METHOD REMOVED]  {m['sig']}  (was line {m['lineno']})")
            for mkey in sorted(a_methods.keys() - b_methods.keys()):
                m = a_methods[mkey]
                violations.append(f"[METHOD ADDED]    {m['sig']}  (now line {m['lineno']})")
            for mkey in sorted(b_methods.keys() & a_methods.keys()):
                bm = b_methods[mkey]
                am = a_methods[mkey]
                if bm["sig"] != am["sig"]:
                    violations.append(f"[SIG CHANGED]     before: {bm['sig']}")
                    violations.append(f"                  after:  {am['sig']}")
    return violations

def check_try_blocks(before_tree: ast.Module, after_tree: ast.Module) -> List[str]:
    before_locs = extract_try_locations(before_tree)
    after_locs = extract_try_locations(after_tree)
    before_sigs = extract_except_signatures(before_tree)
    after_sigs = extract_except_signatures(after_tree)
    violations = []
    before_count = len(before_locs)
    after_count = len(after_locs)
    if after_count < before_count:
        removed = before_count - after_count
        violations.append(f"[TRY REMOVED]     {removed} try/except block(s) removed (before: {before_count}, after: {after_count})")
    for i, (before_loc, after_loc) in enumerate(zip(sorted(before_locs), sorted(after_locs))):
        before_types = before_sigs.get(before_loc, [])
        after_types = after_sigs.get(after_loc, [])
        if is_downgrade(before_types, after_types):
            violations.append(f"[EXCEPT BROADENED] try block #{i+1} at line {before_loc[0]}: before={before_types}, after={after_types}")
    return violations

def check_sec001_injection(tree: ast.Module, filename: str = "<unknown>") -> List[str]:
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("eval", "exec"):
            violations.append(
                f"[SEC-001] [{filename}:{node.lineno}] 检测到 {func.id}() 动态调用 — 高危注入风险"
            )
        elif isinstance(func, ast.Attribute) and func.attr in ("eval", "exec"):
            violations.append(
                f"[SEC-001] [{filename}:{node.lineno}] 检测到 *.{func.attr}() 动态调用 — 高危注入风险"
            )
        elif isinstance(func, ast.Name) and func.id == "getattr":
            if len(node.args) >= 2:
                second_arg = node.args[1]
                if not isinstance(second_arg, ast.Constant) or not isinstance(second_arg.value, str):
                    violations.append(
                        f"[SEC-001] [{filename}:{node.lineno}] 检测到 getattr() 第二参数为动态值 — 属性注入风险"
                    )
    return violations

# ============================================================
# SEC-002：secrets / 凭据硬编码检测（优先级4，BLOCK 级）
# ============================================================
_SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'(?i)(api[_\-]?key|apikey)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}'), "API Key"),
    (re.compile(r'(?i)(access[_\-]?token|auth[_\-]?token|bearer)\s*[=:]\s*["\']?[A-Za-z0-9_\-\.]{16,}'), "Token"),
    (re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\'][^"\']{6,}["\']'), "Password"),
    (re.compile(r'(?i)(secret[_\-]?key|client[_\-]?secret)\s*[=:]\s*["\'][^"\']{6,}["\']'), "Secret"),
    (re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'), "PEM Private Key"),
    (re.compile(r'(?i)(connection[_\-]?string|connstr|dsn)\s*[=:]\s*["\'][^"\']{10,}["\']'), "Connection String"),
    (re.compile(r'(?i)(aws_secret|aws_access_key_id)\s*[=:]\s*["\']?[A-Za-z0-9+/]{16,}'), "AWS Credential"),
    (re.compile(r'ghp_[A-Za-z0-9]{36}'), "GitHub Token"),
    (re.compile(r'sk-[A-Za-z0-9]{32,}'), "OpenAI Key"),
]

def check_sec002_secrets(source: str, filename: str = "<unknown>") -> List[str]:
    violations = []
    for lineno, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue  # 跳过注释行
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(line):
                violations.append(
                    f"[SEC-002] [{filename}:{lineno}] 检测到疑似 {label} 硬编码 — secrets 泄露风险"
                )
                break  # 每行只报一次
    return violations

# ============================================================
# SEC-003：危险调用检测（DENY 级）
# ============================================================
def check_sec003_dangerous(tree: ast.Module, filename: str = "<unknown>") -> List[str]:
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # subprocess(..., shell=True)
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                violations.append(
                    f"[SEC-003] [{filename}:{node.lineno}] 检测到 shell=True — 命令注入风险"
                )
        # yaml.load(...)  →  建议 safe_load
        if isinstance(func, ast.Attribute) and func.attr == "load":
            if isinstance(func.value, ast.Name) and func.value.id == "yaml":
                violations.append(
                    f"[SEC-003] [{filename}:{node.lineno}] 检测到 yaml.load() — 建议改用 yaml.safe_load()"
                )
        # pickle.loads / pickle.load
        if isinstance(func, ast.Attribute) and func.attr in ("loads", "load"):
            if isinstance(func.value, ast.Name) and func.value.id == "pickle":
                violations.append(
                    f"[SEC-003] [{filename}:{node.lineno}] 检测到 pickle.{func.attr}() — 反序列化风险"
                )
    return violations

# ============================================================
# 主函数
# ============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Post-change AST validator (V7.0 分级处置).")
    parser.add_argument("target_file", nargs="?", default=None)
    parser.add_argument("--ref",    default="HEAD")
    parser.add_argument("--before", default=None)
    parser.add_argument("--after",  default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--base",   default="main")
    args = parser.parse_args()

    # ------ 批量模式（--branch）------
    if args.branch is not None:
        if args.target_file is not None or args.before is not None or args.after is not None:
            emit_fatal("批量模式 (--branch) 不能与其他文件参数同时使用。")

        changed_files = get_changed_python_files(args.base, args.branch)
        if not changed_files:
            print(json.dumps({"status": "PASSED", "severity": None, "sec001_hit": False, "violations": []}))
            emit_pass()

        all_violations = []
        for target_file in changed_files:
            sensitive = is_sensitive_path(target_file)
            try:
                before_src = get_source_git(target_file, args.base, sensitive)
                after_src  = get_source_file(target_file, sensitive)
                before_tree = parse_source(before_src, "before")
                after_tree  = parse_source(after_src,  "after")
                violations = []
                violations.extend(check_imports(before_tree, after_tree))
                violations.extend(check_interfaces(before_tree, after_tree))
                violations.extend(check_try_blocks(before_tree, after_tree))
                violations.extend(check_sec001_injection(after_tree, target_file))
                violations.extend(check_sec002_secrets(after_src,  target_file))
                violations.extend(check_sec003_dangerous(after_tree, target_file))
                if violations:
                    prefix = f"[SENSITIVE] [{target_file}]" if sensitive else f"[{target_file}]"
                    all_violations.extend([f"{prefix} {v}" for v in violations])
            except SystemExit as e:
                if e.code == 2:
                    msg = f"解析失败{'（敏感路径 → AUDIT-DENY）' if sensitive else '，跳过此文件'}"
                    all_violations.append(f"[{target_file}] {msg}")
                else:
                    raise e
            except Exception as e:
                all_violations.append(f"[{target_file}] 处理异常: {str(e)}")

        emit_result(all_violations)

    # ------ 单文件模式 ------
    else:
        # 修复 V6.0 缩进错误：--before/--after 分支
        if args.before is not None or args.after is not None:
            if args.before is None or args.after is None:
                emit_fatal("差异模式需要同时提供 --before 和 --after 参数。")
            if args.target_file is not None:
                emit_fatal("不能同时使用位置参数 target_file 和 --before/--after。")
        else:
            if args.target_file is None:
                emit_fatal(
                    "请提供以下两种方式之一：\n"
                    "  1. target_file [--ref REF]\n"
                    "  2. --before 原始文件 --after 修改后文件"
                )

        target_name = args.target_file or args.after or "<unknown>"
        sensitive   = is_sensitive_path(target_name)

        before_src, after_src = resolve_sources(args, sensitive)
        before_tree = parse_source(before_src, "before")
        after_tree  = parse_source(after_src,  "after")

        violations = []
        violations.extend(check_imports(before_tree, after_tree))
        violations.extend(check_interfaces(before_tree, after_tree))
        violations.extend(check_try_blocks(before_tree, after_tree))
        violations.extend(check_sec001_injection(after_tree, target_name))
        violations.extend(check_sec002_secrets(after_src,   target_name))
        violations.extend(check_sec003_dangerous(after_tree, target_name))

        emit_result(violations, is_sensitive=sensitive)


if __name__ == "__main__":
    main()