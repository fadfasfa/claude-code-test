#!/usr/bin/env python3
import sys, json, os
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
CONTRACT = '.ai_workflow/current_contract.json'
SECRET_FILE = '.ai_workflow/.secret_key'
def main():
    if os.path.exists(SECRET_FILE): os.remove(SECRET_FILE)
    with open(CONTRACT, 'r', encoding='utf-8') as f: data = json.load(f)
    if 'task_input_from_claude' not in data: data['task_input_from_claude'] = {}
    data['task_input_from_claude']['executor_node'] = 'QWEN_API'
    if 'signature' in data: del data['signature']
    with open(CONTRACT, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
    print('🔴 拦截已打开，Qwen 零信任模式就绪。')
if __name__ == '__main__': main()
