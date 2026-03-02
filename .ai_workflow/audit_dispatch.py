import os
import sys
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv(".ai_workflow/.env")

def get_gemini_response(prompt, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-2.5-pro"))
    response = model.generate_content(prompt)
    return response.text

def main():
    diff_content = os.popen("git diff HEAD").read()
    if not diff_content:
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
                print(f"[WARNING] Free tier quota possibly exhausted or error, switching to paid tier...")
            else:
                print(f"[ERROR] All auditors offline: {str(e)}")
                sys.exit(1)

if __name__ == "__main__":
    main()