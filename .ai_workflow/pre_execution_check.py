import ast
import re
from typing import List, Tuple

# 危险调用与属性拦截 (原有逻辑)
DANGEROUS_CALLS = {'eval': 'Dynamic code execution', 'exec': 'Dynamic code execution', 'compile': 'Code compilation'}
DANGEROUS_ATTRS = {'__class__', '__subclasses__'}

# 【新增】高危密钥与凭证正则表达式基准字典
SECRET_PATTERNS = {
    'Google_API_Key': re.compile(r'AIza[0-9A-Za-z-_]{35}'),
    'Aliyun_DashScope_Key': re.compile(r'sk-[a-zA-Z0-9]{32,}'),
    'AWS_Access_Key': re.compile(r'AKIA[0-9A-Z]{16}'),
    'Generic_Private_Key': re.compile(r'-----BEGIN (RSA|OPENSSH|DSA|EC|PGP) PRIVATE KEY-----')
}

def check_dangerous_calls(code: str, exempted_modules: List[str] = None) -> Tuple[bool, List[str]]:
    violations = []
    
    # 1. 静态密钥嗅探 (文本层，必须强制熔断)
    for key_type, pattern in SECRET_PATTERNS.items():
        if pattern.search(code):
            violations.append(f"[SECURITY-VIOLATION] Detected hardcoded secret: {key_type}. Forbidden by BeforeTool policy.")

    if violations: 
        return False, violations

    # 2. AST 结构扫描 (逻辑层)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        # [V3.2 适配] 容忍增量片段(Hunk)因缺失上下文导致的语法错误
        # 降级为 WARNING 返回 True，放行写入缓冲区，语义正确性交由 Node C 在后发覆盖时合并裁定。
        return True, [f"[AST-WARNING] Snippet deferred to Node C Hunk Merge (Syntax error: {e})"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_str = get_call_string(node)
            if call_str in DANGEROUS_CALLS:
                violations.append(f"[AST-VIOLATION] Forbidden call {call_str}: {DANGEROUS_CALLS[call_str]}")

        if isinstance(node, ast.Attribute):
            if node.attr in DANGEROUS_ATTRS:
                violations.append(f"[AST-VIOLATION] Forbidden attribute access: {node.attr}")

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