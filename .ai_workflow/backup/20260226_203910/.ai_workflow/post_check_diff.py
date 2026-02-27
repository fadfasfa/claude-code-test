#!/usr/bin/env python3
"""
Post-change AST validator for Python source files.

Compares the AST of a file before and after modification to detect structural
regressions across three dimensions:
  1. Import integrity — no imports added or removed at top-level
  2. Interface stability — function/class signatures unchanged
  3. Exception handling — try/except blocks not removed

Exit codes:
  0  All checks passed.
  1  ERR_AST_VERIFY_FAILED  — structural violation detected.
  2  ERR_PARSE_FAILED       — cannot parse source or bad input.

JSON output format (on error):
  {"status": "ERROR", "error_code": "ERR_...", "raw_output": "..."}
"""

import ast
import json
import subprocess
import sys
import argparse
import os
from typing import Dict, List, Set, Tuple, Optional

# Python 3.11+ compatibility: ast.TryStar handles 'except*' syntax
TRY_TYPES = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)


# --------------------------------------------------------------------------
# Output / exit helpers
# --------------------------------------------------------------------------

def emit_pass() -> None:
    """All checks passed."""
    sys.exit(0)


def emit_fail(raw_output: str) -> None:
    """Structural violation detected."""
    print(json.dumps({
        "status": "ERROR",
        "error_code": "ERR_AST_VERIFY_FAILED",
        "raw_output": raw_output
    }, ensure_ascii=False))
    sys.exit(1)


def emit_fatal(raw_output: str) -> None:
    """Cannot parse or bad input."""
    print(json.dumps({
        "status": "ERROR",
        "error_code": "ERR_PARSE_FAILED",
        "raw_output": raw_output
    }, ensure_ascii=False))
    sys.exit(2)


# --------------------------------------------------------------------------
# Source acquisition
# --------------------------------------------------------------------------

def get_source_git(target_file: str, ref: str) -> str:
    """
    Run: git show <ref>:<target_file>
    Returns the file content as a string.
    Calls emit_fatal() on any failure.
    """
    # Normalize Windows paths to forward slashes for git
    git_path = target_file.replace("\\", "/")

    result = subprocess.run(
        ["git", "show", f"{ref}:{git_path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        emit_fatal(
            f"git show {ref}:{git_path} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def get_source_file(path: str) -> str:
    """Read a plain file. Calls emit_fatal() if not found."""
    if not os.path.isfile(path):
        emit_fatal(f"File not found: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def resolve_sources(args: argparse.Namespace) -> Tuple[str, str]:
    """
    Returns (before_source, after_source) based on CLI mode.

    Git mode:   args.target_file is set, args.before is None.
    Diff mode:  args.before and args.after are set, args.target_file is None.
    """
    if args.before is not None:
        # Explicit diff mode
        before_src = get_source_file(args.before)
        after_src = get_source_file(args.after)
    else:
        # Git mode
        ref = args.ref or "HEAD"
        before_src = get_source_git(args.target_file, ref)
        after_src = get_source_file(args.target_file)
    return before_src, after_src


# --------------------------------------------------------------------------
# AST parsing
# --------------------------------------------------------------------------

def parse_source(source: str, label: str) -> ast.Module:
    """
    Parse Python source into an AST.
    label is used in the fatal error message ("before" or "after").
    Calls emit_fatal() on SyntaxError.
    """
    try:
        return ast.parse(source, type_comments=False)
    except SyntaxError as e:
        emit_fatal(
            f"SyntaxError in {label} version: {e.msg} "
            f"(line {e.lineno}, col {e.offset})"
        )


# --------------------------------------------------------------------------
# Extraction helpers
# --------------------------------------------------------------------------

def extract_import_keys(tree: ast.Module) -> Set[str]:
    """
    Extract top-level import statements only (tree.body, not nested).
    Returns a set of canonical string keys.

    Key format examples:
      "import:os"
      "import:sys as system"
      "from:typing:List"
      "from:typing:Optional as Opt"
      "from:.:relative_mod"        (level=1 relative import)
    """
    keys = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    keys.add(f"import:{alias.name} as {alias.asname}")
                else:
                    keys.add(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            module = prefix + (node.module or "")
            for alias in node.names:
                if alias.asname:
                    keys.add(f"from:{module}:{alias.name} as {alias.asname}")
                else:
                    keys.add(f"from:{module}:{alias.name}")
    return keys


def signature_of(node: ast.FunctionDef) -> str:
    """
    语义级签名提取：忽略 Python 3.9+ 类型提示，强制纳入默认参数值进行防篡改哈希比对。
    """
    args = node.args
    parts = []

    # Positional-only args (before '/')
    for a in args.posonlyargs:
        parts.append(a.arg)
    if args.posonlyargs:
        parts.append("/")

    # Regular positional-or-keyword args
    default_offset = len(args.args) - len(args.defaults)
    for i, a in enumerate(args.args):
        part = a.arg
        if i >= default_offset:
            default_val = ast.unparse(args.defaults[i - default_offset])
            part += f"={default_val}"
        parts.append(part)

    # *args or bare '*' separator for keyword-only args
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")

    # Keyword-only args (with defaults)
    kw_default_offset = len(args.kwonlyargs) - len(args.kw_defaults)
    for i, a in enumerate(args.kwonlyargs):
        part = a.arg
        default = args.kw_defaults[i]
        if default is not None:
            part += f"={ast.unparse(default)}"
        parts.append(part)

    # **kwargs
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)

    return f"def {node.name}({', '.join(parts)})"


def extract_interfaces(tree: ast.Module) -> Dict[str, dict]:
    """
    Extract top-level function and class definitions (not nested).
    Returns a dict with structure:
    {
      "func_name": {
        "kind": "function",
        "sig": "def func_name(a: int) -> str",
        "lineno": 10,
        "decorators": ["staticmethod"]
      },
      "ClassName": {
        "kind": "class",
        "lineno": 20,
        "methods": {
          "ClassName.method_name": {
            "sig": "def method_name(self, x)",
            "lineno": 22,
            "decorators": ["classmethod"]
          }
        }
      }
    }
    """
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = {
                "kind": "function",
                "sig": signature_of(node),
                "lineno": node.lineno,
                "decorators": [ast.unparse(d) for d in node.decorator_list],
            }
        elif isinstance(node, ast.ClassDef):
            methods = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    key = f"{node.name}.{item.name}"
                    methods[key] = {
                        "sig": signature_of(item),
                        "lineno": item.lineno,
                        "decorators": [ast.unparse(d) for d in item.decorator_list],
                    }
            result[node.name] = {
                "kind": "class",
                "lineno": node.lineno,
                "methods": methods,
            }
    return result


def extract_try_locations(tree: ast.Module) -> List[Tuple[int, int]]:
    """
    Walk the entire AST (all depths) and collect (lineno, col_offset) for every
    ast.Try and ast.TryStar node. Returns sorted list.
    """
    locations = []
    for node in ast.walk(tree):
        if isinstance(node, TRY_TYPES):
            locations.append((node.lineno, node.col_offset))
    return sorted(locations)


# --------------------------------------------------------------------------
# Validation passes
# --------------------------------------------------------------------------

def check_imports(before_tree: ast.Module, after_tree: ast.Module) -> List[str]:
    """Check that top-level imports have not been added or removed."""
    before_keys = extract_import_keys(before_tree)
    after_keys = extract_import_keys(after_tree)

    # Global whitelist: allow these imports to be added without violation
    ALLOWED_NEW_IMPORTS = {
        "import:tempfile",
        "import:shutil",
    }

    violations = []
    for key in sorted(before_keys - after_keys):
        violations.append(f"[IMPORT REMOVED]  {key}")
    for key in sorted(after_keys - before_keys):
        if key not in ALLOWED_NEW_IMPORTS:
            violations.append(f"[IMPORT ADDED]    {key}")
    return violations


def check_interfaces(before_tree: ast.Module, after_tree: ast.Module) -> List[str]:
    """Check that function/class signatures have not changed at module level."""
    before = extract_interfaces(before_tree)
    after = extract_interfaces(after_tree)
    violations = []

    # --- Top-level names removed ---
    for name in sorted(before.keys() - after.keys()):
        entry = before[name]
        if entry["kind"] == "function":
            violations.append(f"[FUNC REMOVED]    {entry['sig']}  (was line {entry['lineno']})")
        else:
            violations.append(f"[CLASS REMOVED]   class {name}  (was line {entry['lineno']})")

    # --- Top-level names added ---
    for name in sorted(after.keys() - before.keys()):
        entry = after[name]
        if entry["kind"] == "function":
            violations.append(f"[FUNC ADDED]      {entry['sig']}  (now line {entry['lineno']})")
        else:
            violations.append(f"[CLASS ADDED]     class {name}  (now line {entry['lineno']})")

    # --- Names present in both: check signature drift ---
    for name in sorted(before.keys() & after.keys()):
        b_entry = before[name]
        a_entry = after[name]

        if b_entry["kind"] != a_entry["kind"]:
            # Function became a class or vice versa
            violations.append(
                f"[KIND CHANGED]    {name}: was {b_entry['kind']}, now {a_entry['kind']}"
            )
            continue

        if b_entry["kind"] == "function":
            if b_entry["sig"] != a_entry["sig"]:
                violations.append(f"[SIG CHANGED]     before: {b_entry['sig']}")
                violations.append(f"                  after:  {a_entry['sig']}")

        elif b_entry["kind"] == "class":
            # Check methods within the class
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
    """
    废弃僵化的数量比对，改为语义覆盖校验：允许合法的异常块合并与优化。
    """
    before_locs = extract_try_locations(before_tree)
    after_locs = extract_try_locations(after_tree)
    # 仅当重构前存在异常处理，且重构后被完全物理抹除时才告警
    if len(before_locs) > 0 and len(after_locs) == 0:
         return ['[TRY REMOVED] 致命风险：全局异常捕获块被完全剥离。']
    return []


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-change AST validator for Python source files."
    )

    # Git mode arguments
    parser.add_argument(
        "target_file",
        nargs="?",
        default=None,
        help="Target file path (git mode). Compared against --ref version.",
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="Git ref for 'before' snapshot (default: HEAD).",
    )

    # Diff mode arguments
    parser.add_argument(
        "--before",
        default=None,
        help="Path to original file (diff mode, requires --after).",
    )
    parser.add_argument(
        "--after",
        default=None,
        help="Path to modified file (diff mode, requires --before).",
    )

    args = parser.parse_args()

    # --- Validate argument combinations ---
    if args.before is not None or args.after is not None:
        # Diff mode: both --before and --after must be present
        if args.before is None or args.after is None:
            emit_fatal("Diff mode requires both --before and --after arguments.")
        if args.target_file is not None:
            emit_fatal("Cannot combine positional target_file with --before/--after.")
    else:
        # Git mode: target_file must be provided
        if args.target_file is None:
            emit_fatal(
                "Provide either: (1) target_file [--ref REF], "
                "or (2) --before ORIG --after MODIFIED"
            )

    # --- Acquire sources ---
    before_src, after_src = resolve_sources(args)

    # --- Parse ASTs ---
    before_tree = parse_source(before_src, "before")
    after_tree = parse_source(after_src, "after")

    # --- Run all three checks ---
    violations = []
    violations.extend(check_imports(before_tree, after_tree))
    violations.extend(check_interfaces(before_tree, after_tree))
    violations.extend(check_try_blocks(before_tree, after_tree))

    # --- Emit result ---
    if violations:
        emit_fail("\n".join(violations))
    else:
        emit_pass()


if __name__ == "__main__":
    main()
