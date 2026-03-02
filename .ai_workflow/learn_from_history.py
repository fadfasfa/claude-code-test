#!/usr/bin/env python3
import os
import sys
import json
import ast
import logging
import subprocess
import traceback
from collections import defaultdict
from datetime import datetime

# 【Python 技术规范】强异常捕获与纯 ASCII 日志记录
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger("LearnEngineV2")

DANGEROUS_IMPORTS = {'os', 'subprocess', 'sys', 'pickle'}
DANGEROUS_CALLS = {'eval', 'exec', 'compile'}

def scan_ast_for_risk(filepath: str) -> bool:
    if not os.path.exists(filepath):
        return False
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in DANGEROUS_IMPORTS: return True
            elif isinstance(node, ast.ImportFrom):
                if node.module in DANGEROUS_IMPORTS: return True
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in DANGEROUS_CALLS: return True
    except Exception as e:
        logger.debug(f"[INFO] AST parse skip {filepath}: {e}")
        return True # Fail-Safe 原则
    return False

def get_git_fragility(commit_count: int = 100) -> dict:
    fragility_scores = defaultdict(int)
    try:
        cmd = ["git", "log", f"-{commit_count}", "--name-only", "--pretty=format:COMMIT_MSG:%s"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8', errors='replace')

        current_is_fragile = False
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith("COMMIT_MSG:"):
                msg = line[11:].lower()
                current_is_fragile = any(kw in msg for kw in ['fix', 'bug', 'revert', 'crash', 'hotfix'])
            elif line.endswith('.py'):
                if current_is_fragile:
                    fragility_scores[line] += 1
    except subprocess.CalledProcessError:
        logger.warning("[WARNING] Git environment error or no history, skipping fragility trace.")
    except Exception as e:
        logger.error(f"[ERROR] Git history read exception:\n{traceback.format_exc()}")
    return fragility_scores

def generate_baseline():
    try:
        logger.info("[START] Initiating multidimensional awareness engine (V2)...")
        if not os.path.exists(".git"):
            logger.warning("[WARNING] .git directory not found, partial AST baseline only.")

        fragility = get_git_fragility()

        py_files = []
        for root, dirs, files in os.walk('.'):
            if any(d in root for d in ['.git', '__pycache__', 'node_modules', 'venv', '.venv']):
                continue
            for f in files:
                if f.endswith('.py'):
                    path = os.path.normpath(os.path.join(root, f)).replace('\\', '/')
                    py_files.append(path)

        suggestions = {}
        for f in py_files:
            if f.startswith('.ai_workflow/') or f.startswith('scripts/'):
                suggestions[f] = "STRICT"
                continue
            if scan_ast_for_risk(f):
                suggestions[f] = "STRICT"
                continue
            if 'test' in f.lower() or f.startswith('docs/'):
                suggestions[f] = "LOOSE"
                continue
            if fragility.get(f, 0) >= 2:
                suggestions[f] = "STANDARD"
                continue
            suggestions[f] = "STANDARD"

        learned_patterns = {
            "last_learned": datetime.now().isoformat(),
            "engine_version": "V2.0_AST_GIT_AWARE_SAFE",
            "file_security_suggestions": suggestions
        }

        output_path = ".ai_workflow/learned_patterns.json"
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(learned_patterns, f, indent=2, ensure_ascii=False)

        logger.info(f"[SUCCESS] Dynamic learning complete! Baseline saved to: {output_path}")

    except Exception as e:
        logger.error(f"[FATAL] Learning engine crashed:\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    generate_baseline()
