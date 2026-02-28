import ast
from typing import List, Tuple

DANGEROUS_CALLS = {
    'os.system': '执行系统命令', 'os.popen': '执行系统命令',
    'subprocess.call': '执行子进程', 'subprocess.run': '执行子进程',
    'subprocess.Popen': '执行子进程',
    'eval': '动态执行代码',
    'exec': '动态执行代码', 'compile': '编译代码',
    '__import__': '动态导入', 'pickle.loads': '不安全的反序列化',
    'pickle.load': '不安全的反序列化'
}

# 豁免模块列表：允许某些模块在特定文件中使用
EXEMPTED_MODULES = []

def check_dangerous_calls(code: str, exempted_modules: List[str] = None) -> Tuple[bool, List[str]]:
    exempted_modules = exempted_modules if exempted_modules is not None else EXEMPTED_MODULES
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"语法错误：{e}"]

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_str = get_call_string(node)
            if call_str in DANGEROUS_CALLS:
                module_name = get_module_name(call_str)
                if module_name and module_name in exempted_modules:
                    continue
                violations.append(f"禁止调用 {call_str}: {DANGEROUS_CALLS[call_str]}")

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ['subprocess'] and alias.name not in exempted_modules:
                    violations.append(f"禁止高危导入：{alias.name}")

        if isinstance(node, ast.ImportFrom):
            if node.module == 'subprocess' and node.module not in exempted_modules:
                violations.append(f"禁止从高危模块导入：{node.module}")
            elif node.module == 'os':
                dangerous_os = {'system', 'popen'}
                for alias in node.names:
                    if (alias.name in dangerous_os or alias.name == '*') and 'os' not in exempted_modules:
                        violations.append(f"禁止从 os 导入高危函数：{alias.name}")

    return len(violations) == 0, violations

def get_call_string(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    elif isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
    return ""

def get_module_name(call_str: str) -> str:
    """Extract module name from call string (e.g., 'subprocess.run' -> 'subprocess')"""
    if '.' in call_str:
        return call_str.split('.')[0]
    return ""
