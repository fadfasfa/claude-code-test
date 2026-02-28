import ast
from typing import List, Tuple

DANGEROUS_CALLS = {
    'os.system': 'System command execution',
    'os.popen': 'System command execution',
    'subprocess.call': 'Subprocess execution',
    'subprocess.run': 'Subprocess execution',
    'subprocess.Popen': 'Subprocess execution',
    'eval': 'Dynamic code execution',
    'exec': 'Dynamic code execution',
    'compile': 'Code compilation',
    '__import__': 'Dynamic import',
    'getattr': 'Dynamic attribute access (Potential Sandbox Escape)',
    'setattr': 'Dynamic attribute modification',
    'delattr': 'Dynamic attribute deletion',
    'globals': 'Global namespace access',
    'locals': 'Local namespace access',
    'pickle.loads': 'Insecure deserialization',
    'pickle.load': 'Insecure deserialization'
}

DANGEROUS_ATTRS = {
    '__class__', '__subclasses__', '__bases__', '__mro__', '__globals__', '__builtins__'
}

EXEMPTED_MODULES = []

def check_dangerous_calls(code: str, exempted_modules: List[str] = None) -> Tuple[bool, List[str]]:
    exempted_modules = exempted_modules if exempted_modules is not None else EXEMPTED_MODULES
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"[AST-ERROR] Syntax error: {e}"]

    violations = []
    for node in ast.walk(tree):
        # 1. 拦截高危函数调用 (包含反射与执行)
        if isinstance(node, ast.Call):
            call_str = get_call_string(node)
            if call_str in DANGEROUS_CALLS:
                violations.append(f"[AST-VIOLATION] Forbidden call {call_str}: {DANGEROUS_CALLS[call_str]}")

        # 2. 拦截底层反射属性访问 (切断沙箱逃逸核心路径)
        if isinstance(node, ast.Attribute):
            if node.attr in DANGEROUS_ATTRS:
                violations.append(f"[AST-VIOLATION] Forbidden attribute access: {node.attr}")

        # 3. 拦截高危模块导入
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ['subprocess', 'os', 'sys', 'pickle'] and alias.name not in exempted_modules:
                    violations.append(f"[AST-VIOLATION] Forbidden import: {alias.name}")

        if isinstance(node, ast.ImportFrom):
            if node.module in ['subprocess', 'os', 'sys', 'pickle'] and node.module not in exempted_modules:
                violations.append(f"[AST-VIOLATION] Forbidden import from: {node.module}")

    return len(violations) == 0, violations

def get_call_string(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    elif isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
    return ""