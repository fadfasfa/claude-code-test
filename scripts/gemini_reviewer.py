import os
import sys
import subprocess

# [Win-Encoding-Safe] Force UTF-8 for Windows compatibility
if sys.platform == 'win32':
    try:
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

def invoke_gemini_cli_ast(target_file):
    """
    Contract 2: CLI Awakening Engine for AST Review
    - Build cross-platform environment variables
    - Construct non-interactive (YOLO) CLI command
    - Execute and capture output
    - Parse closed-loop gateway
    """

    # 1. Validate target file exists
    if not os.path.isfile(target_file):
        print(f"[ERROR] Target file not found: {target_file}")
        sys.exit(1)

    # 2. Build cross-platform environment variables
    cli_env = os.environ.copy()
    cli_env['PYTHONIOENCODING'] = 'utf-8'
    cli_env['LANG'] = 'zh_CN.UTF-8'

    # 2.5. Load .env file and inject API key authentication
    env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.ai_workflow', '.env')
    gemini_api_key = None

    if os.path.isfile(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    # Priority: GEMINI_FREE_KEY > GEMINI_PAID_KEY
                    if key == 'GEMINI_FREE_KEY' and value:
                        gemini_api_key = value
                        break
                    elif key == 'GEMINI_PAID_KEY' and value and not gemini_api_key:
                        gemini_api_key = value
        except Exception as e:
            print(f"[WARNING] Failed to parse .env file: {e}")

    if not gemini_api_key:
        print("[ERROR] No valid GEMINI_FREE_KEY or GEMINI_PAID_KEY found in .env file")
        print("[ERROR] AST review authentication failed - cannot proceed.")
        sys.exit(1)

    cli_env['GEMINI_API_KEY'] = gemini_api_key

    # 3. Build non-interactive CLI command
    # Format: gemini "/code-review @<target_file>" --yolo
    command = [
        "gemini",
        f"/code-review @{target_file}",
        "--yolo"
    ]

    print(f"[AUDIT] Executing AST review for: {target_file}")

    # 4. Execute and capture output
    try:
        # Windows platform compatibility: add shell=True for cmd execution
        subprocess_kwargs = {
            'env': cli_env,
            'capture_output': True,
            'text': True,
            'encoding': 'utf-8',
            'errors': 'replace'
        }
        if sys.platform == 'win32':
            subprocess_kwargs['shell'] = True

        result = subprocess.run(
            command,
            **subprocess_kwargs
        )

        output = result.stdout if result.stdout else result.stderr

        print("-" * 50)
        print(output)
        print("-" * 50)

        # 5. Parse closed-loop gateway
        if "[FAIL]" in output or "致命逻辑漏洞" in output or result.returncode != 0:
            print(f"[BLOCKED] AST review failed for {target_file}")
            sys.exit(1)
        else:
            print(f"[PASSED] AST review passed for {target_file}")
            sys.exit(0)

    except FileNotFoundError:
        print("[ERROR] gemini-cli tool not found in PATH. Please ensure gemini CLI is installed.")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Unhandled exception during AST review: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/gemini_reviewer.py <target_file>")
        sys.exit(1)

    target_file = sys.argv[1]
    invoke_gemini_cli_ast(target_file)
