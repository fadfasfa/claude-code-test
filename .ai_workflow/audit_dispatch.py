import os
import sys
import subprocess
from google import genai
from dotenv import load_dotenv

# 【Win-Encoding-Safe】强制终端输出为 UTF-8，彻底解决 print(中文) 引发的 ascii/gbk 崩溃
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

load_dotenv(".ai_workflow/.env")

def get_gemini_response(prompt, api_key):
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-pro"),
        contents=prompt,
    )
    return response.text

def main():
    result = subprocess.run(
        ["git", "diff", "HEAD"], 
        capture_output=True, 
        text=True, 
        encoding='utf-8', 
        errors='replace'
    )
    diff_content = result.stdout

    if not diff_content.strip():
        print("[INFO] No code changes detected, skipping audit.")
        return

    audit_prompt = f"""
    你现在是高级安全审计员。请审查以下代码变动：
    {diff_content}
    
    要求：
    1. 严禁修改 .ai_workflow 和 .git/hooks 目录。
    2. 检查逻辑漏洞、后门或 Windows 兼容性问题。
    3. 仅输出 [PASS] 或 [FAIL: 原因]。
    """

    keys = [os.getenv("GEMINI_FREE_KEY"), os.getenv("GEMINI_PAID_KEY")]
    
    for i, key in enumerate(keys):
        if not key: continue
        try:
            mode = "FREE_TIER" if i == 0 else "PAID_TIER"
            print(f"[AUDIT] Using Gemini {mode}...")
            result = get_gemini_response(audit_prompt, key)
            
            print("-" * 30)
            print(result)
            print("-" * 30)
            
            if "[FAIL]" in result:
                sys.exit(1)
            else:
                print("[SUCCESS] Audit passed.")
                sys.exit(0)
                
        except Exception as e:
            if i == 0:
                print(f"[WARNING] Free tier error or quota exhausted, switching to paid tier...")
            else:
                print(f"[ERROR] All auditors offline: {str(e)}")
                sys.exit(1)

if __name__ == "__main__":
    main()