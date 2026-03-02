import ast
from typing import List, Tuple

# 【放权版】仅拦截致命的动态执行与底层类反射，允许 os, sys 等正常业务操作
DANGEROUS_CALLS = {
    'eval': 'Dynamic code execution',
    'exec': 'Dynamic code execution',
    'compile': 'Code compilation'
}

DANGEROUS_ATTRS = {
    '__class__', '__subclasses__'
}

def check_dangerous_calls(code: str, exempted_modules: List[str] = None) -> Tuple[bool, List[str]]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"[AST-ERROR] Syntax error: {e}"]

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_str = get_call_string(node)
            if call_str in DANGEROUS_CALLS:
                violations.append(f"[AST-VIOLATION] Forbidden call {call_str}: {DANGEROUS_CALLS[call_str]}")

        if isinstance(node, ast.Attribute):
            if node.attr in DANGEROUS_ATTRS:
                violations.append(f"[AST-VIOLATION] Forbidden attribute access: {node.attr}")

        # 彻底放开了对 os / sys 的拦截，现在只拦截 subprocess 和 pickle
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ['subprocess', 'pickle'] and (not exempted_modules or alias.name not in exempted_modules):
                    violations.append(f"[AST-VIOLATION] Forbidden import: {alias.name}")

        if isinstance(node, ast.ImportFrom):
            if node.module in ['subprocess', 'pickle'] and (not exempted_modules or node.module not in exempted_modules):
                violations.append(f"[AST-VIOLATION] Forbidden import from: {node.module}")

    return len(violations) == 0, violations

def get_call_string(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    elif isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
    return ""