#!/usr/bin/env python3
"""
Post-change AST validator for Python source files.
[V6.0 - 记录模式：扫描结果写入 event_log，不阻断提交]

变更说明（V5.0 -> V6.0）：
  - 移除提交阻断逻辑（emit_fail 不再触发 exit 1）
  - 扫描结果统一由调用方（post-commit hook）写入 event_log.jsonl
  - SEC-001 命中时输出结构化 JSON 供 hook 识别并写入 yellow_cards
  - 保留全部检测逻辑：AST diff + SEC-001 eval/exec/getattr 注入检测
"""

import ast
import json
import subprocess
import sys
import argparse
import os
from typing import Dict, List, Set, Tuple, Optional

TRY_TYPES = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)

def emit_pass() -> None:
    sys.exit(0)

def emit_result(violations: List[str]) -> None:
    """V6.0: 输出结构化结果，exit 0 表示无违规，exit 1 表示有违规（供 hook 读取，不再是阻断信号）。"""
    has_sec001 = any("[SEC-001]" in v for v in violations)
    print(json.dumps({
        "status": "WARNING" if violations else "PASSED",
        "sec001_hit": has_sec001,
        "violations": violations
    }, ensure_ascii=False))
    sys.exit(1 if violations else 0)

def emit_fatal(raw_output: str) -> None:
    print(json.dumps({
        "status": "PARSE_ERROR",
        "error_code": "ERR_PARSE_FAILED",
        "raw_output": raw_output
    }, ensure_ascii=False))
    sys.exit(2)

def get_source_git(target_file: str, ref: str) -> str:
    git_path = target_file.replace("\\", "/")
    result = subprocess.run(
        ["git", "show", f"{ref}:{git_path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        if "fatal: Path" in result.stderr or "exists on disk, but not in" in result.stderr:
            return ""
        emit_fatal(f"git show {ref}:{git_path} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout

def get_source_file(path: str) -> str:
    if not os.path.isfile(path): emit_fatal(f"File not found: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as f: return f.read()

def resolve_sources(args: argparse.Namespace) -> Tuple[str, str]:
    if args.before is not None:
        return get_source_file(args.before), get_source_file(args.after)
    else:
        ref = args.ref or "HEAD"
        return get_source_git(args.target_file, ref), get_source_file(args.target_file)

def parse_source(source: str, label: str) -> ast.Module:
    try:
        return ast.parse(source, type_comments=False)
    except SyntaxError:
        return ast.Module(body=[], type_ignores=[])

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
            result[node.name] = {
                "kind": "class", "lineno": node.lineno, "methods": methods,
            }
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
            emit_fatal(f"git diff failed: {result.stderr.strip()}")
        files = []
        for line in result.stdout.strip().split('\n'):
            if line.strip() and line.strip().endswith('.py'):
                files.append(line.strip().replace("\\", "/"))
        return files
    except Exception as e:
        emit_fatal(f"Failed to get changed Python files: {str(e)}")

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-change AST validator (V6.0 记录模式).")
    parser.add_argument("target_file", nargs="?", default=None)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--before", default=None)
    parser.add_argument("--after", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--base", default="main")
    args = parser.parse_args()

    if args.branch is not None:
        if args.target_file is not None or args.before is not None or args.after is not None:
            emit_fatal("批量模式 (--branch) 不能与其他文件参数同时使用。")

        changed_files = get_changed_python_files(args.base, args.branch)
        if not changed_files:
            print(json.dumps({"status": "PASSED", "sec001_hit": False, "violations": []}))
            emit_pass()

        all_violations = []
        for target_file in changed_files:
            try:
                before_src = get_source_git(target_file, args.base)
                after_src = get_source_file(target_file)
                before_tree = parse_source(before_src, "before")
                after_tree = parse_source(after_src, "after")
                violations = []
                violations.extend(check_imports(before_tree, after_tree))
                violations.extend(check_interfaces(before_tree, after_tree))
                violations.extend(check_try_blocks(before_tree, after_tree))
                violations.extend(check_sec001_injection(after_tree, target_file))
                if violations:
                    all_violations.extend([f"[{target_file}] {v}" for v in violations])
            except SystemExit as e:
                if e.code == 2:
                    all_violations.append(f"[{target_file}] 解析失败，跳过此文件。")
                else:
                    raise e
            except Exception as e:
                all_violations.append(f"[{target_file}] 处理异常: {str(e)}")

        emit_result(all_violations)

    else:
        if args.before is not None or args.after is not None:
            if args.before is None or args.after is None:
                emit_fatal("Diff mode requires both --before and --after arguments.")
            if args.target_file is not None:
                emit_fatal("Cannot combine positional target_file with --before/--after.")
        else:
            if args.target_file is None:
                emit_fatal("Provide either: (1) target_file [--ref REF], or (2) --before ORIG --after MODIFIED")

        before_src, after_src = resolve_sources(args)
        before_tree = parse_source(before_src, "before")
        after_tree = parse_source(after_src, "after")

        violations = []
        violations.extend(check_imports(before_tree, after_tree))
        violations.extend(check_interfaces(before_tree, after_tree))
        violations.extend(check_try_blocks(before_tree, after_tree))
        target_name = args.target_file or args.after or "<unknown>"
        violations.extend(check_sec001_injection(after_tree, target_name))

        emit_result(violations)


if __name__ == "__main__":
    main()
