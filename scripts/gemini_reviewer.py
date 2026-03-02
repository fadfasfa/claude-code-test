import os
import sys
import subprocess
import time
from pathlib import Path
from dotenv import load_dotenv
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

# 1. Load protected environment variables from.ai_workflow
WORKFLOW_DIR = Path(__file__).parent.parent / '.ai_workflow'
load_dotenv(dotenv_path=WORKFLOW_DIR / '.env')

# 2. Build API Pool (Filter out empty keys)
API_POOL =
API_POOL = [k for k in API_POOL if k and k.strip()]

if not API_POOL:
    print(" No Gemini API keys found in.ai_workflow/.env")
    sys.exit(1)

def generate_patch_with_fallback(prompt, model_name='gemini-2.5-pro'):
    """Rotates through API keys to handle Free Tier 429 limits"""
    for attempt, current_key in enumerate(API_POOL):
        try:
            genai.configure(api_key=current_key)
            # Instructing Gemini 2.5 Pro to act as a strict code gatekeeper
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction="You are a strict code reviewer. Analyze the python file and error traceback. Return ONLY the fully corrected code. NO markdown formatting. NO explanations."
            )
            print(f"[*] Requesting Gemini 2.5 Pro API (Using Key Index {attempt})...")
            response = model.generate_content(prompt)
            return response.text.replace('```python', '').replace('```', '').strip()
            
        except ResourceExhausted:
            print(f"[!] Key Index {attempt} rate limit (429) hit. Falling back to next key...")
            continue # Move to Paid Key if Free is exhausted
        except Exception as e:
            print(f"[!] Unhandled exception: {e}")
            break
            
    raise Exception(" All API keys exhausted. Halting review loop.")

def run_and_review(target_file, max_retries=3):
    print(f"[*] Starting auto-audit loop for: {target_file}")
    
    for attempt in range(max_retries):
        result = subprocess.run(["python", target_file], capture_output=True, text=True)
        
        if result.returncode == 0:
            print("[+] Code executed successfully. Review loop closed.")
            return
            
        print(f"[-] Attempt {attempt+1} Failed. Capturing logs and sending to Gemini...")
        error_log = result.stderr if result.stderr else result.stdout
        
        with open(target_file, "r", encoding="utf-8") as f:
            code_content = f.read()

        prompt = f"File: {target_file}\nCode:\n{code_content}\nError:\n{error_log}"
        
        try:
            new_code = generate_patch_with_fallback(prompt)
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(new_code)
            print("[+] Patch applied. Retrying test...")
        except Exception as e:
            print(str(e))
            break
            
    print("[!] Max retries reached. Please check manually.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/gemini_reviewer.py <target_script>")
    else:
        run_and_review(sys.argv[1])