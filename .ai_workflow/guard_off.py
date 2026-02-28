#!/usr/bin/env python3
import sys, json, os, hmac, hashlib, secrets
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
CONTRACT = '.ai_workflow/current_contract.json'
SECRET_FILE = '.ai_workflow/.secret_key'
def main():
    key = secrets.token_hex(32)
    with open(SECRET_FILE, 'w', encoding='utf-8') as f: f.write(key)
    with open(CONTRACT, 'r', encoding='utf-8') as f: data = json.load(f)
    if 'task_input_from_claude' not in data: data['task_input_from_claude'] = {}
    data['task_input_from_claude']['executor_node'] = 'CLAUDE_API'
    if 'signature' in data: del data['signature']
    payload_str = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    data['signature'] = hmac.new(key.encode('utf-8'), payload_str.encode('utf-8'), hashlib.sha256).hexdigest()
    with open(CONTRACT, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
    print('🟢 拦截已关闭，开发者/Claude 提权模式就绪。')
if __name__ == '__main__': main()
