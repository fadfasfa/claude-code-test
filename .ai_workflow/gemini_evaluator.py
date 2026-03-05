#!/usr/bin/env python3
"""
Gemini Evaluator - Google AI Studio API 自动化审查模块

实现 Reflect-Refine 循环：
1. Reflect: 分析代码变更，识别潜在问题
2. Refine: 生成修复建议，迭代优化
3. Final Verdict: 通过/打回

支持：
- 指数退避重试机制
- 多轮对话上下文保持
- 结构化 JSON 输出解析
- API 配额管理与故障转移
"""

import os
import sys
import json
import time
import hashlib
import subprocess
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum

# 第三方依赖：使用 httpx 进行 HTTP 请求（比 requests 更现代，支持异步）
try:
    import httpx
except ImportError:
    httpx = None

# 备用：如果 httpx 不可用，使用 urllib
if httpx is None:
    try:
        import urllib.request
        import urllib.error
    except ImportError:
        pass


class VerdictType(Enum):
    """审查裁决类型"""
    PASS = "PASS"                    # 通过，无问题
    NEEDS_MINOR_FIX = "NEEDS_MINOR_FIX"  # 需要小修复（可自动修复）
    NEEDS_MAJOR_REVIEW = "NEEDS_MAJOR_REVIEW"  # 需要大修改（人工审查）
    REJECT = "REJECT"                # 拒绝，严重问题


class ErrorCode(Enum):
    """错误代码枚举"""
    SUCCESS = "SUCCESS"
    ERR_API_FAILURE = "ERR_API_FAILURE"
    ERR_RATE_LIMITED = "ERR_RATE_LIMITED"
    ERR_INVALID_RESPONSE = "ERR_INVALID_RESPONSE"
    ERR_PARSE_FAILED = "ERR_PARSE_FAILED"
    ERR_TIMEOUT = "ERR_TIMEOUT"
    ERR_QUOTA_EXCEEDED = "ERR_QUOTA_EXCEEDED"


@dataclass
class ReviewResult:
    """单次审查结果"""
    verdict: VerdictType
    confidence: float  # 0.0 - 1.0
    issues: List[str]
    suggestions: List[str]
    auto_fixable: List[Dict[str, Any]]  # 可自动修复的问题
    requires_human_review: bool
    raw_response: str
    error_code: Optional[ErrorCode] = None


@dataclass
class ReflectRefineState:
    """Reflect-Refine 循环状态"""
    iteration: int
    max_iterations: int
    cumulative_issues: List[str]
    resolved_issues: List[str]
    current_assessment: Optional[ReviewResult]
    history: List[Dict[str, Any]]


# 默认配置
DEFAULT_CONFIG = {
    "api_endpoint": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
    "api_key_env": "GEMINI_API_KEY",
    "fallback_api_keys": [],  # 备用 API Key 列表
    "max_retries": 5,
    "base_delay": 1.0,  # 基础延迟（秒）
    "max_delay": 60.0,  # 最大延迟（秒）
    "timeout": 120.0,   # 请求超时（秒）
    "max_refine_iterations": 3,
    "temperature": 0.3,
    "top_p": 0.8,
}


class GeminiEvaluator:
    """
    Gemini Evaluator - 使用 Google AI Studio API 进行代码审查

    实现 Reflect-Refine 循环：
    1. Reflect: 分析代码变更，识别问题
    2. Refine: 基于反馈生成修复建议
    3. Final Verdict: 综合评估是否通过
    """

    # 系统提示词模板
    SYSTEM_PROMPT = """你是一个专业的代码审查专家，专注于 Python 代码的安全性、正确性和可维护性。

你的任务是：
1. 分析代码变更（diff），识别潜在问题
2. 评估问题的严重程度
3. 提供具体的修复建议
4. 判断是否需要人工审查

审查维度：
- 安全性：是否有安全漏洞（注入、命令执行、路径遍历等）
- 正确性：逻辑是否正确，边界条件是否处理
- 可维护性：代码是否清晰，是否有适当的错误处理
- 性能：是否有明显的性能问题

输出格式必须是有效的 JSON：
{
    "verdict": "PASS" | "NEEDS_MINOR_FIX" | "NEEDS_MAJOR_REVIEW" | "REJECT",
    "confidence": 0.0-1.0,
    "issues": ["问题描述 1", "问题描述 2", ...],
    "suggestions": ["修复建议 1", "修复建议 2", ...],
    "auto_fixable": [
        {
            "type": "import_removal" | "dangerous_call_removal" | "exception_handling",
            "location": "文件路径：行号",
            "description": "问题描述",
            "suggested_fix": "建议的修复代码"
        }
    ],
    "requires_human_review": true/false,
    "reasoning": "审查推理过程简述"
}"""

    REFLECT_PROMPT = """请分析以下代码变更：

## 文件：{file_path}

## 变更内容：
```diff
{diff_content}
```

## 原始代码（变更前行）：
```python
{old_code}
```

## 新代码（变更后）：
```python
{new_code}
```

请进行 Reflect 分析：
1. 识别所有潜在问题（安全、逻辑、风格）
2. 评估每个问题的严重程度
3. 判断是否引入了危险调用（eval, exec, subprocess, pickle 等）
4. 检查异常处理是否充分
5. 确认接口签名是否保持一致

以 JSON 格式输出你的分析。"""

    REFINE_PROMPT = """基于上一轮审查结果，以下是需要进一步分析的问题：

## 上一轮识别的问题：
{previous_issues}

## 作者/开发者的修复尝试或说明：
{developer_response}

请进行 Refine 分析：
1. 评估修复是否充分
2. 识别是否引入了新问题
3. 判断当前状态是否可以接受
4. 如果仍有问题，提供更具体的修复指导

以 JSON 格式输出你的分析。"""

    FINAL_VERDICT_PROMPT = """综合以下审查历史，给出最终裁决：

## 审查历史：
{review_history}

## 当前代码状态：
```python
{current_code}
```

请给出最终裁决（Verdict）：
- PASS: 代码无问题，可以接受
- NEEDS_MINOR_FIX: 有小问题但可自动修复
- NEEDS_MAJOR_REVIEW: 需要人工审查
- REJECT: 有严重问题，必须修改

以 JSON 格式输出最终裁决。"""

    def __init__(self, config: Dict[str, Any] = None):
        """初始化 Evaluator"""
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.api_key = self._load_api_key()
        self._client_cache = None

    def _load_api_key(self) -> str:
        """从环境或配置加载 API Key"""
        # 优先从环境变量获取
        api_key = os.environ.get(self.config["api_key_env"])
        if api_key:
            return api_key

        # 尝试从 LiteLLM 配置加载
        litellm_config = ".ai_workflow/litellm-config.yaml"
        if os.path.exists(litellm_config):
            try:
                import yaml
                with open(litellm_config, 'r', encoding='utf-8') as f:
                    config_data = yaml.safe_load(f)
                for model in config_data.get('model_list', []):
                    if model.get('model_name') == 'gemini-2.5-pro':
                        key = model.get('litellm_params', {}).get('api_key', '')
                        if key and not key.startswith('AIza') and '填入' not in key:
                            return key
                        elif key.startswith('AIza'):
                            return key  # 返回免费 API key
            except Exception:
                pass

        # 尝试从 manifest 加载
        manifest_path = ".ai_workflow/manifest.json"
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    manifest = json.load(f)
                api_key = manifest.get('claude_sudo', {}).get('api_key', '')
                if api_key:
                    return api_key
            except Exception:
                pass

        return ""

    def _get_client(self):
        """获取 HTTP 客户端"""
        if httpx:
            if self._client_cache is None:
                self._client_cache = httpx.Client(
                    timeout=httpx.Timeout(self.config["timeout"]),
                    follow_redirects=True
                )
            return self._client_cache
        return None

    def _call_api(self, messages: List[Dict[str, str]], max_retries: int = None) -> Tuple[Optional[str], Optional[ErrorCode]]:
        """
        调用 Gemini API

        返回：(response_text, error_code)
        """
        max_retries = max_retries or self.config["max_retries"]
        api_key = self.api_key

        if not api_key:
            return None, ErrorCode.ERR_API_FAILURE

        endpoint = self.config["api_endpoint"]

        # 分离 system 和非 system 消息（Gemini 用 systemInstruction 字段）
        system_content = ""
        user_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_content = msg["content"]
            else:
                user_messages.append(msg)

        payload = {
            "generationConfig": {
                "temperature": self.config["temperature"],
                "top_p": self.config["top_p"],
                "response_mime_type": "application/json"  # 强制 JSON 输出
            }
        }

        # 如果有 system prompt，使用 systemInstruction 字段
        if system_content:
            payload["systemInstruction"] = {
                "parts": [{"text": system_content}]
            }

        # 使用正确的 user/model role 交替映射
        payload["contents"] = [
            {
                "role": msg.get("role", "user"),
                "parts": [{"text": msg["content"]}]
            }
            for msg in user_messages
        ]

        last_error = None

        for attempt in range(max_retries):
            try:
                if httpx:
                    client = self._get_client()
                    response = client.post(
                        f"{endpoint}?key={api_key}",
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    )

                    if response.status_code == 429:
                        # 速率限制，指数退避
                        retry_after = int(response.headers.get("Retry-After", 1))
                        delay = min(retry_after, self.config["max_delay"])
                        time.sleep(delay)
                        continue

                    if response.status_code == 403:
                        # 配额超出
                        return None, ErrorCode.ERR_QUOTA_EXCEEDED

                    if response.status_code != 200:
                        last_error = f"API returned status {response.status_code}: {response.text[:200]}"
                        continue

                    result = response.json()
                    candidates = result.get("candidates", [])
                    if not candidates:
                        last_error = "No candidates in response"
                        continue

                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        return text, None
                    else:
                        last_error = "Empty parts in response"
                        continue

                else:
                    # 使用 urllib 作为后备
                    import urllib.request
                    import urllib.error
                    import json as json_mod

                    url = f"{endpoint}?key={api_key}"
                    data = json_mod.dumps(payload).encode('utf-8')
                    req = urllib.request.Request(
                        url,
                        data=data,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )

                    with urllib.request.urlopen(req, timeout=int(self.config["timeout"])) as resp:
                        result = json_mod.loads(resp.read().decode('utf-8'))
                        candidates = result.get("candidates", [])
                        if candidates:
                            content = candidates[0].get("content", {})
                            parts = content.get("parts", [])
                            if parts:
                                text = parts[0].get("text", "")
                                return text, None

                    last_error = "Invalid response structure"

            except Exception as e:
                last_error = str(e)

                # [修订版] 指数退避与 Full Jitter (防惊群效应)
                if attempt < max_retries - 1:
                    import random
                    # 计算上限: min(T_max, t_0 * (2^n))
                    cap = min(self.config["max_delay"], self.config["base_delay"] * (2 ** attempt))
                    # Full Jitter 核心公式: t = random(0, cap)
                    delay = random.uniform(0, cap)
                    print(f"[API_BACKOFF] Rate limit or error encountered. Full Jitter backing off for {delay:.2f}s...")
                    time.sleep(delay)

        # 所有重试失败
        print(f"[API ERROR] All retries failed. Last error: {last_error}")
        return None, ErrorCode.ERR_API_FAILURE

    def _parse_json_response(self, response_text: str) -> Optional[Dict[str, Any]]:
        """解析 JSON 响应，处理可能的格式问题"""
        if not response_text:
            return None

        # 尝试直接解析
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON（可能在 markdown 代码块中）
        import re
        json_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
        match = re.search(json_pattern, response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试提取独立 JSON 对象
        brace_count = 0
        start = -1
        for i, char in enumerate(response_text):
            if char == '{':
                if brace_count == 0:
                    start = i
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0 and start != -1:
                    try:
                        return json.loads(response_text[start:i+1])
                    except json.JSONDecodeError:
                        pass
                    start = -1

        return None

    def reflect(self, file_path: str, diff_content: str, old_code: str, new_code: str) -> ReviewResult:
        """
        Reflect 阶段：分析代码变更

        参数：
            file_path: 文件路径
            diff_content: diff 内容
            old_code: 原始代码
            new_code: 新代码
        """
        prompt = self.REFLECT_PROMPT.format(
            file_path=file_path,
            diff_content=diff_content,
            old_code=old_code[:8000] if old_code else "",  # 限制长度
            new_code=new_code[:8000]  # 限制长度
        )

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        response_text, error = self._call_api(messages)

        if error:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.5,
                issues=[f"API 调用失败：{error.value}"],
                suggestions=["请稍后重试或联系管理员"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=error
            )

        parsed = self._parse_json_response(response_text)

        if not parsed:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.3,
                issues=["无法解析 AI 响应"],
                suggestions=["检查 API 配置后重试"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=ErrorCode.ERR_INVALID_RESPONSE
            )

        # 提取并验证字段
        try:
            verdict_str = parsed.get("verdict", "NEEDS_MAJOR_REVIEW")
            verdict = VerdictType(verdict_str) if verdict_str in [v.value for v in VerdictType] else VerdictType.NEEDS_MAJOR_REVIEW

            return ReviewResult(
                verdict=verdict,
                confidence=float(parsed.get("confidence", 0.5)),
                issues=parsed.get("issues", []),
                suggestions=parsed.get("suggestions", []),
                auto_fixable=parsed.get("auto_fixable", []),
                requires_human_review=parsed.get("requires_human_review", True),
                raw_response=response_text or "",
                error_code=None
            )
        except Exception as e:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.3,
                issues=[f"解析响应失败：{e}"],
                suggestions=["检查响应格式"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=ErrorCode.ERR_PARSE_FAILED
            )

    def refine(self, previous_result: ReviewResult, developer_response: str) -> ReviewResult:
        """
        Refine 阶段：基于修复尝试重新评估

        参数：
            previous_result: 上一轮审查结果
            developer_response: 开发者修复说明或新代码
        """
        previous_issues_json = json.dumps({
            "verdict": previous_result.verdict.value,
            "issues": previous_result.issues,
            "suggestions": previous_result.suggestions
        }, ensure_ascii=False)

        prompt = self.REFINE_PROMPT.format(
            previous_issues=previous_issues_json,
            developer_response=developer_response
        )

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        response_text, error = self._call_api(messages)

        if error:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.5,
                issues=[f"Refine API 调用失败：{error.value}"],
                suggestions=["请稍后重试"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=error
            )

        parsed = self._parse_json_response(response_text)

        if not parsed:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.3,
                issues=["无法解析 Refine 响应"],
                suggestions=["检查 API 配置"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=ErrorCode.ERR_INVALID_RESPONSE
            )

        try:
            verdict_str = parsed.get("verdict", "NEEDS_MAJOR_REVIEW")
            verdict = VerdictType(verdict_str) if verdict_str in [v.value for v in VerdictType] else VerdictType.NEEDS_MAJOR_REVIEW

            return ReviewResult(
                verdict=verdict,
                confidence=float(parsed.get("confidence", 0.5)),
                issues=parsed.get("issues", []),
                suggestions=parsed.get("suggestions", []),
                auto_fixable=parsed.get("auto_fixable", []),
                requires_human_review=parsed.get("requires_human_review", True),
                raw_response=response_text or "",
                error_code=None
            )
        except Exception as e:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.3,
                issues=[f"解析 Refine 响应失败：{e}"],
                suggestions=["检查响应格式"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=ErrorCode.ERR_PARSE_FAILED
            )

    def final_verdict(self, review_history: List[ReviewResult], current_code: str) -> ReviewResult:
        """
        最终裁决阶段

        参数：
            review_history: 审查历史
            current_code: 当前代码
        """
        history_summary = []
        for i, result in enumerate(review_history):
            history_summary.append({
                "iteration": i + 1,
                "verdict": result.verdict.value,
                "issues": result.issues,
                "confidence": result.confidence
            })

        prompt = self.FINAL_VERDICT_PROMPT.format(
            review_history=json.dumps(history_summary, ensure_ascii=False),
            current_code=current_code[:10000]  # 限制长度
        )

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        response_text, error = self._call_api(messages)

        if error:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.5,
                issues=[f"Final Verdict API 调用失败：{error.value}"],
                suggestions=["请稍后重试或人工审查"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=error
            )

        parsed = self._parse_json_response(response_text)

        if not parsed:
            # 尝试从响应中提取裁决
            if response_text:
                if "PASS" in response_text.upper():
                    return ReviewResult(
                        verdict=VerdictType.PASS,
                        confidence=0.6,
                        issues=[],
                        suggestions=[],
                        auto_fixable=[],
                        requires_human_review=False,
                        raw_response=response_text,
                        error_code=None
                    )
                elif "REJECT" in response_text.upper():
                    return ReviewResult(
                        verdict=VerdictType.REJECT,
                        confidence=0.6,
                        issues=["AI 判定代码需要修改"],
                        suggestions=["检查代码并修复问题"],
                        auto_fixable=[],
                        requires_human_review=True,
                        raw_response=response_text,
                        error_code=None
                    )

            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.3,
                issues=["无法解析最终裁决"],
                suggestions=["人工审查代码"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=ErrorCode.ERR_INVALID_RESPONSE
            )

        try:
            verdict_str = parsed.get("verdict", "NEEDS_MAJOR_REVIEW")
            verdict = VerdictType(verdict_str) if verdict_str in [v.value for v in VerdictType] else VerdictType.NEEDS_MAJOR_REVIEW

            return ReviewResult(
                verdict=verdict,
                confidence=float(parsed.get("confidence", 0.5)),
                issues=parsed.get("issues", []),
                suggestions=parsed.get("suggestions", []),
                auto_fixable=parsed.get("auto_fixable", []),
                requires_human_review=parsed.get("requires_human_review", True),
                raw_response=response_text or "",
                error_code=None
            )
        except Exception as e:
            return ReviewResult(
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                confidence=0.3,
                issues=[f"解析最终裁决失败：{e}"],
                suggestions=["人工审查代码"],
                auto_fixable=[],
                requires_human_review=True,
                raw_response=response_text or "",
                error_code=ErrorCode.ERR_PARSE_FAILED
            )

    def reflect_refine_cycle(
        self,
        file_path: str,
        diff_content: str,
        old_code: str,
        new_code: str,
        max_iterations: int = None
    ) -> Tuple[ReviewResult, ReflectRefineState]:
        """
        执行 Reflect-Refine 循环

        参数：
            file_path: 文件路径
            diff_content: diff 内容
            old_code: 原始代码
            new_code: 新代码
            max_iterations: 最大迭代次数

        返回：
            (最终审查结果，循环状态)
        """
        max_iterations = max_iterations or self.config["max_refine_iterations"]

        state = ReflectRefineState(
            iteration=0,
            max_iterations=max_iterations,
            cumulative_issues=[],
            resolved_issues=[],
            current_assessment=None,
            history=[]
        )

        # 第一次 Reflect
        print(f"[GEMINI] Starting Reflect phase for {file_path}...")
        result = self.reflect(file_path, diff_content, old_code, new_code)

        state.iteration = 1
        state.current_assessment = result
        state.cumulative_issues = result.issues.copy()
        state.history.append({
            "phase": "reflect",
            "result": asdict(result) if hasattr(result, '__dataclass_fields__') else result.__dict__
        })

        # 如果第一次审查就 PASS，直接返回
        if result.verdict == VerdictType.PASS:
            print(f"[GEMINI] Review PASSED on first iteration.")
            return result, state

        # 如果需要人工审查或拒绝，不进入 Refine 循环
        if result.verdict in [VerdictType.REJECT, VerdictType.NEEDS_MAJOR_REVIEW]:
            print(f"[GEMINI] Review requires human review. Verdict: {result.verdict.value}")
            return result, state

        # 进入 Refine 循环
        for iteration in range(2, max_iterations + 1):
            print(f"[GEMINI] Entering Refine iteration {iteration}...")

            # 模拟开发者响应（这里可以实际修改代码后重新传入）
            developer_response = "开发者已根据建议进行修复，请重新评估。"

            refine_result = self.refine(result, developer_response)

            state.iteration = iteration
            state.current_assessment = refine_result

            # 记录问题变化
            for issue in result.issues:
                if issue not in refine_result.issues:
                    state.resolved_issues.append(issue)
            for issue in refine_result.issues:
                if issue not in state.cumulative_issues:
                    state.cumulative_issues.append(issue)

            state.history.append({
                "phase": "refine",
                "iteration": iteration,
                "result": asdict(refine_result) if hasattr(refine_result, '__dataclass_fields__') else refine_result.__dict__
            })

            # 如果 PASS 或达到最大迭代次数，退出循环
            if refine_result.verdict == VerdictType.PASS or iteration >= max_iterations:
                break

            result = refine_result

        # 最终裁决
        print(f"[GEMINI] Computing final verdict...")
        final_result = self.final_verdict([state.current_assessment], new_code)

        state.history.append({
            "phase": "final_verdict",
            "result": asdict(final_result) if hasattr(final_result, '__dataclass_fields__') else final_result.__dict__
        })

        return final_result, state


def get_diff_content(file_path: str, ref: str = "HEAD") -> str:
    """获取文件的 diff 内容"""
    git_path = file_path.replace("\\", "/")

    result = subprocess.run(
        ["git", "diff", f"{ref}..", "--", git_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode != 0:
        return ""

    return result.stdout


def get_file_at_ref(file_path: str, ref: str = "HEAD") -> Optional[str]:
    """获取指定 ref 的文件内容"""
    git_path = file_path.replace("\\", "/")

    result = subprocess.run(
        ["git", "show", f"{ref}:{git_path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode != 0:
        return None

    return result.stdout


def main():
    """CLI 入口点"""
    import argparse

    parser = argparse.ArgumentParser(description="Gemini Evaluator - AI 代码审查")
    parser.add_argument("file", help="要审查的文件路径")
    parser.add_argument("--ref", default="HEAD", help="Git ref 作为比较基准")
    parser.add_argument("--max-iterations", type=int, default=3, help="最大 Refine 迭代次数")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    args = parser.parse_args()

    # 初始化 Evaluator
    evaluator = GeminiEvaluator()

    if not evaluator.api_key:
        print("[ERROR] 未找到 API Key，请设置 GEMINI_API_KEY 环境变量或在配置文件中提供")
        sys.exit(1)

    # 获取 diff 和代码
    diff_content = get_diff_content(args.file, args.ref)
    new_code = get_file_at_ref(args.file, "HEAD") or ""
    old_code = get_file_at_ref(args.file, args.ref) or ""

    if not diff_content and not new_code:
        print(f"[ERROR] 文件 {args.file} 没有变更或不存在")
        sys.exit(1)

    # 执行 Reflect-Refine 循环
    result, state = evaluator.reflect_refine_cycle(
        file_path=args.file,
        diff_content=diff_content,
        old_code=old_code,
        new_code=new_code,
        max_iterations=args.max_iterations
    )

    # 输出结果
    if args.json:
        output = {
            "verdict": result.verdict.value,
            "confidence": result.confidence,
            "issues": result.issues,
            "suggestions": result.suggestions,
            "requires_human_review": result.requires_human_review,
            "review_state": asdict(state)
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"文件：{args.file}")
        print(f"裁决：{result.verdict.value}")
        print(f"置信度：{result.confidence:.2f}")
        print(f"需要人工审查：{'是' if result.requires_human_review else '否'}")

        if result.issues:
            print(f"\n发现的问题 ({len(result.issues)}):")
            for i, issue in enumerate(result.issues, 1):
                print(f"  {i}. {issue}")

        if result.suggestions:
            print(f"\n修复建议 ({len(result.suggestions)}):")
            for i, suggestion in enumerate(result.suggestions, 1):
                print(f"  {i}. {suggestion}")

        print(f"{'='*60}\n")

    # 根据裁决设置退出码
    if result.verdict == VerdictType.PASS:
        sys.exit(0)
    elif result.verdict == VerdictType.NEEDS_MINOR_FIX:
        sys.exit(10)  # 自定义退出码
    elif result.verdict == VerdictType.NEEDS_MAJOR_REVIEW:
        sys.exit(11)
    else:  # REJECT
        sys.exit(12)


if __name__ == "__main__":
    main()
