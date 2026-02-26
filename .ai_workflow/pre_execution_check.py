import ast
from typing import List, Tuple

DANGEROUS_CALLS = {
    'os.system': '执行系统命令', 'os.popen': '执行系统命令',
    'subprocess.call': '执行子进程', 'subprocess.run': '执行子进程',
    'subprocess.Popen': '执行子进程', 'eval': '动态执行代码',
    'exec': '动态执行代码', 'compile': '编译代码',
    '__import__': '动态导入', 'pickle.loads': '不安全的反序列化',
    'pickle.load': '不安全的反序列化'
}

def check_dangerous_calls(code: str) -> Tuple[bool, List[str]]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"语法错误: {e}"]

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_str = get_call_string(node)
            if call_str in DANGEROUS_CALLS:
                violations.append(f"禁止调用 {call_str}: {DANGEROUS_CALLS[call_str]}")

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ['os', 'subprocess']:
                    violations.append(f"禁止高危导入: {alias.name}")

    return len(violations) == 0, violations

def get_call_string(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return f"{node.func.value.id}.{node.func.attr}"
    elif isinstance(node.func, ast.Name):
        return node.func.id
    return ""
