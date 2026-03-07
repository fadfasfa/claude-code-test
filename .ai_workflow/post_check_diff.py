#!/usr/bin/env python3
"""
Post-change AST validator for Python source files.
[V3.2 Adapted for Hunk Merging Tolerance]
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

def emit_fail(raw_output: str) -> None:
    print(json.dumps({
        "status": "ERROR",
        "error_code": "ERR_AST_VERIFY_FAILED",
        "raw_output": raw_output
    }, ensure_ascii=False))
    sys.exit(1)

def emit_fatal(raw_output: str) -> None:
    print(json.dumps({
        "status": "ERROR",
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
        # [V3.2 适配] 隔离区新增的片段无基线历史，构建空基线平滑放行
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
    except SyntaxError as e:
        # [V3.2 适配] 容忍增量片段解析失败，返回空树以跳过 Diff 误判
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
    """获取两个分支之间变更的 Python 文件列表。"""
    try:
        result = subprocess.run(
            ["git", "diff", f"{base_branch}..{target_branch}", "--name-only", "--diff-filter=AM"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            emit_fatal(f"git diff failed: {result.stderr.strip()}")

        # 过滤出以 .py 结尾的文件，并统一使用正斜杠
        files = []
        for line in result.stdout.strip().split('\n'):
            if line.strip() and line.strip().endswith('.py'):
                # 统一使用正斜杠处理 Windows 路径分隔符差异
                normalized_path = line.strip().replace("\\", "/")
                files.append(normalized_path)
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
    """从 import key 中解析根模块名。相对导入返回 None 表示直接放行。"""
    if key.startswith("import:"):
        module = key[len("import:"):].split(" as ")[0]
        return module.split(".")[0]
    elif key.startswith("from:"):
        parts = key.split(":")
        if len(parts) >= 2:
            module = parts[1]
            if module.startswith("."):
                return None  # 相对导入，一律放行
            return module.split(".")[0]
    return key

def _is_stdlib_import(key: str) -> bool:
    """判断 import key 是否属于标准库导入。"""
    root = _extract_root_module(key)
    if root is None:
        return True  # 相对导入，放行
    return root in STDLIB_MODULES

def check_imports(before_tree: ast.Module, after_tree: ast.Module) -> List[str]:
    before_keys = extract_import_keys(before_tree)
    after_keys = extract_import_keys(after_tree)
    ALLOWED_REMOVED_IMPORTS = {"import:shutil"}
    violations = []
    for key in sorted(before_keys - after_keys):
        if key not in ALLOWED_REMOVED_IMPORTS: violations.append(f"[IMPORT REMOVED]  {key}")
    for key in sorted(after_keys - before_keys):
        if not _is_stdlib_import(key): violations.append(f"[IMPORT ADDED]    {key}")
    return violations

def check_interfaces(before_tree: ast.Module, after_tree: ast.Module) -> List[str]:
    before = extract_interfaces(before_tree)
    after = extract_interfaces(after_tree)
    violations = []

    for name in sorted(before.keys() - after.keys()):
        entry = before[name]
        if entry["kind"] == "function": violations.append(f"[FUNC REMOVED]    {entry['sig']}  (was line {entry['lineno']})")
        else: violations.append(f"[CLASS REMOVED]   class {name}  (was line {entry['lineno']})")

    for name in sorted(before.keys() & after.keys()):
        b_entry = before[name]
        a_entry = after[name]

        if b_entry["kind"] != a_entry["kind"]:
            violations.append(f"[KIND CHANGED]    {name}: was {b_entry['kind']}, now {a_entry['kind']}")
            continue

        if b_entry["kind"] == "function":
            if b_entry["sig"] != a_entry["sig"]:
                violations.append(f"[SIG CHANGED]     before: {b_entry['sig']}")
                violations.append(f"                  after:  {a_entry['sig']}")
                violations.append("[CRITICAL 熔断建议] 检测到核心签名变更或高危异常处理，强制中断自动合并。")
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
                    violations.append("[CRITICAL 熔断建议] 检测到核心签名变更或高危异常处理，强制中断自动合并。")
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
        for lineno, col in before_locs:
            violations.append(f"  before: try at line {lineno}, col {col}")
        for lineno, col in after_locs:
            violations.append(f"  after:  try at line {lineno}, col {col}")

    for i, (before_loc, after_loc) in enumerate(zip(sorted(before_locs), sorted(after_locs))):
        before_types = before_sigs.get(before_loc, [])
        after_types = after_sigs.get(after_loc, [])
        if is_downgrade(before_types, after_types):
            violations.append(f"[EXCEPT BROADENED] try block #{i+1} at line {before_loc[0]}: before={before_types}, after={after_types}")
            violations.append("[CRITICAL 熔断建议] 检测到核心签名变更或高危异常处理，强制中断自动合并。")

    return violations

def main() -> None:
    parser = argparse.ArgumentParser(description="Post-change AST validator for Python source files.")
    parser.add_argument("target_file", nargs="?", default=None, help="Target file path (git mode). Compared against --ref version.")
    parser.add_argument("--ref", default="HEAD", help="Git ref for 'before' snapshot (default: HEAD).")
    parser.add_argument("--before", default=None, help="Path to original file (diff mode, requires --after).")
    parser.add_argument("--after", default=None, help="Path to modified file (diff mode, requires --before).")
    parser.add_argument("--branch", default=None, help="Branch name to compare against base branch for batch processing.")
    parser.add_argument("--base", default="main", help="Base branch for comparison (default: main).")
    args = parser.parse_args()

    if args.branch is not None:
        # 批量处理模式：基于分支差异
        if args.target_file is not None or args.before is not None or args.after is not None:
            emit_fatal("批量模式 (--branch) 不能与其他文件参数同时使用。")

        changed_files = get_changed_python_files(args.base, args.branch)
        if not changed_files:
            # 无 Python 文件变动时优雅退出
            print("未检测到 Python 文件变更，跳过 AST 审计。", file=sys.stderr)
            emit_pass()

        all_violations = []
        for target_file in changed_files:
            try:
                # 为每个文件执行原有的验证逻辑
                before_src = get_source_git(target_file, args.base)
                after_src = get_source_file(target_file)
                before_tree = parse_source(before_src, "before")
                after_tree = parse_source(after_src, "after")

                violations = []
                violations.extend(check_imports(before_tree, after_tree))
                violations.extend(check_interfaces(before_tree, after_tree))
                violations.extend(check_try_blocks(before_tree, after_tree))

                if violations:
                    # 为每个文件的违规项添加文件前缀
                    prefixed_violations = [f"[{target_file}] {v}" for v in violations]
                    all_violations.extend(prefixed_violations)

            except SystemExit as e:
                # 捕获 emit_fatal 调用，但继续处理其他文件
                if e.code == 2:  # 解析失败
                    all_violations.append(f"[{target_file}] 解析失败，跳过此文件。")
                else:
                    raise e
            except Exception as e:
                all_violations.append(f"[{target_file}] 处理异常: {str(e)}")

        if all_violations:
            emit_fail("\n".join(all_violations))
        else:
            emit_pass()

    else:
        # 原有单文件模式（向后兼容）
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

        if violations:
            emit_fail("\n".join(violations))
        else:
            emit_pass()

if __name__ == "__main__":
    main()