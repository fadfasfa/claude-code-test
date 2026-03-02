#!/usr/bin/env python3
"""
审查管理器 - Retry & Rejection 机制

封装 Gemini Evaluator，提供：
1. 指数退避重试
2. 多文件并行审查
3. 打回裁决执行
4. 审查报告生成
"""

import os
import sys
import json
import time
import subprocess
import shutil
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, asdict

# 导入 Gemini Evaluator
try:
    from .gemini_evaluator import (
        GeminiEvaluator,
        ReviewResult,
        VerdictType,
        ErrorCode,
        ReflectRefineState,
        get_diff_content,
        get_file_at_ref
    )
except ImportError:
    from gemini_evaluator import (
        GeminiEvaluator,
        ReviewResult,
        VerdictType,
        ErrorCode,
        ReflectRefineState,
        get_diff_content,
        get_file_at_ref
    )


@dataclass
class ReviewDecision:
    """审查决策"""
    file_path: str
    verdict: VerdictType
    passed: bool
    blocked: bool
    requires_human: bool
    issues: List[str]
    suggestions: List[str]
    auto_fixable: List[Dict]
    confidence: float
    iterations: int
    error_code: Optional[ErrorCode]


@dataclass
class BatchReviewResult:
    """批量审查结果"""
    timestamp: str
    total_files: int
    passed_files: int
    blocked_files: int
    human_review_files: int
    decisions: List[ReviewDecision]
    summary: str


class ReviewManager:
    """
    审查管理器

    负责：
    - 批量文件审查
    - 重试逻辑
    - 打回执行
    - 报告生成
    """

    # 打回原因枚举
    REJECT_REASON_CODE = {
        "SECURITY_VIOLATION": "安全违规",
        "INTERFACE_DRIFT": "接口签名变更",
        "EXCEPTION_REMOVAL": "异常处理被删除",
        "DANGEROUS_PATTERN": "危险代码模式",
        "LOW_CONFIDENCE": "AI 置信度过低",
        "API_FAILURE": "API 调用失败",
        "HUMAN_REVIEW_REQUIRED": "需要人工审查"
    }

    def __init__(self, evaluator_config: Dict[str, Any] = None):
        """
        初始化审查管理器

        参数：
            evaluator_config: GeminiEvaluator 配置
        """
        self.evaluator = GeminiEvaluator(evaluator_config)
        self.review_history: List[BatchReviewResult] = []

    def review_file(
        self,
        file_path: str,
        ref: str = "HEAD",
        max_retries: int = 3,
        max_iterations: int = 3
    ) -> ReviewDecision:
        """
        审查单个文件

        参数：
            file_path: 文件路径
            ref: Git ref 作为比较基准
            max_retries: API 失败时最大重试次数
            max_iterations: Reflect-Refine 最大迭代次数

        返回：
            ReviewDecision: 审查决策
        """
        # 获取代码内容
        diff_content = get_diff_content(file_path, ref)
        new_code = get_file_at_ref(file_path, "HEAD") or ""
        old_code = get_file_at_ref(file_path, ref) or ""

        # 如果没有变更，直接通过
        if not diff_content.strip():
            return ReviewDecision(
                file_path=file_path,
                verdict=VerdictType.PASS,
                passed=True,
                blocked=False,
                requires_human=False,
                issues=[],
                suggestions=[],
                auto_fixable=[],
                confidence=1.0,
                iterations=0,
                error_code=None
            )

        last_result: Optional[ReviewResult] = None
        last_error: Optional[ErrorCode] = None

        # 重试循环
        for attempt in range(max_retries):
            try:
                print(f"[REVIEW] Attempting review of {file_path} (attempt {attempt + 1}/{max_retries})...")

                # 执行 Reflect-Refine 循环
                result, state = self.evaluator.reflect_refine_cycle(
                    file_path=file_path,
                    diff_content=diff_content,
                    old_code=old_code,
                    new_code=new_code,
                    max_iterations=max_iterations
                )

                last_result = result

                # 根据裁决决定是否需要重试
                if result.error_code:
                    # API 相关错误，可以重试
                    if result.error_code in [ErrorCode.ERR_API_FAILURE, ErrorCode.ERR_TIMEOUT]:
                        last_error = result.error_code
                        if attempt < max_retries - 1:
                            # 指数退避
                            delay = min(2 ** attempt, 30)
                            print(f"[REVIEW] API error, retrying in {delay}s...")
                            time.sleep(delay)
                            continue
                    elif result.error_code == ErrorCode.ERR_QUOTA_EXCEEDED:
                        # 配额超出，不重试
                        break
                    elif result.error_code in [ErrorCode.ERR_INVALID_RESPONSE, ErrorCode.ERR_PARSE_FAILED]:
                        # 响应解析错误，可以尝试重试
                        if attempt < max_retries - 1:
                            delay = min(2 ** attempt, 10)
                            print(f"[REVIEW] Parse error, retrying in {delay}s...")
                            time.sleep(delay)
                            continue

                # 成功获得有效结果，退出重试循环
                break

            except Exception as e:
                last_error = ErrorCode.ERR_API_FAILURE
                print(f"[REVIEW] Exception during review: {e}")
                if attempt < max_retries - 1:
                    delay = min(2 ** attempt, 10)
                    print(f"[REVIEW] Retrying in {delay}s...")
                    time.sleep(delay)

        # 构建审查决策
        if last_result:
            verdict = last_result.verdict
            passed = (verdict == VerdictType.PASS)
            blocked = (verdict in [VerdictType.REJECT, VerdictType.NEEDS_MAJOR_REVIEW])
            requires_human = last_result.requires_human_review

            # 低置信度也需要人工审查
            if last_result.confidence < 0.4:
                requires_human = True

            return ReviewDecision(
                file_path=file_path,
                verdict=verdict,
                passed=passed,
                blocked=blocked,
                requires_human=requires_human,
                issues=last_result.issues,
                suggestions=last_result.suggestions,
                auto_fixable=last_result.auto_fixable,
                confidence=last_result.confidence,
                iterations=state.iteration if last_result else 0,
                error_code=last_result.error_code or last_error
            )
        else:
            # 所有重试失败
            return ReviewDecision(
                file_path=file_path,
                verdict=VerdictType.NEEDS_MAJOR_REVIEW,
                passed=False,
                blocked=True,
                requires_human=True,
                issues=[f"审查失败：{last_error.value if last_error else '未知错误'}"],
                suggestions=["检查 API 配置或联系管理员"],
                auto_fixable=[],
                confidence=0.0,
                iterations=0,
                error_code=last_error or ErrorCode.ERR_API_FAILURE
            )

    def review_files(
        self,
        file_paths: List[str],
        ref: str = "HEAD",
        max_retries: int = 3,
        max_iterations: int = 3
    ) -> BatchReviewResult:
        """
        批量审查文件

        参数：
            file_paths: 文件路径列表
            ref: Git ref 作为比较基准
            max_retries: API 失败时最大重试次数
            max_iterations: Reflect-Refine 最大迭代次数

        返回：
            BatchReviewResult: 批量审查结果
        """
        decisions: List[ReviewDecision] = []
        passed_count = 0
        blocked_count = 0
        human_review_count = 0

        for file_path in file_paths:
            decision = self.review_file(
                file_path=file_path,
                ref=ref,
                max_retries=max_retries,
                max_iterations=max_iterations
            )
            decisions.append(decision)

            if decision.passed:
                passed_count += 1
            if decision.blocked:
                blocked_count += 1
            if decision.requires_human:
                human_review_count += 1

        # 生成摘要
        summary_parts = []
        if passed_count > 0:
            summary_parts.append(f"{passed_count} 文件通过")
        if blocked_count > 0:
            summary_parts.append(f"{blocked_count} 文件被拦截")
        if human_review_count > 0:
            summary_parts.append(f"{human_review_count} 文件需要人工审查")

        result = BatchReviewResult(
            timestamp=datetime.now().isoformat(),
            total_files=len(file_paths),
            passed_files=passed_count,
            blocked_files=blocked_count,
            human_review_files=human_review_count,
            decisions=decisions,
            summary="; ".join(summary_parts) if summary_parts else "无变更"
        )

        self.review_history.append(result)
        return result

    def execute_rejection(
        self,
        decisions: List[ReviewDecision],
        base_commit: str,
        backup_dir: str = ".ai_workflow/backup"
    ) -> Tuple[bool, str]:
        """
        执行打回操作

        参数：
            decisions: 审查决策列表
            base_commit: 基准 commit
            backup_dir: 备份目录

        返回：
            (是否执行了打回，打回原因)
        """
        blocked_decisions = [d for d in decisions if d.blocked or d.requires_human]

        if not blocked_decisions:
            return False, ""

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"gemini_reject_{timestamp}")
        os.makedirs(backup_path, exist_ok=True)

        # 收集需要打回的文件
        reject_files = [d.file_path for d in blocked_decisions]

        # 生成补丁文件
        patch_file = os.path.join(backup_path, "changes.patch")
        with open(patch_file, "w", encoding="utf-8") as f:
            subprocess.run(
                ["git", "diff", "HEAD", "--"] + reject_files,
                stdout=f,
                text=True
            )

        # 备份当前文件
        for file_path in reject_files:
            if os.path.exists(file_path):
                dest_path = os.path.join(backup_path, file_path.replace("/", "_").replace("\\", "_"))
                os.makedirs(os.path.dirname(dest_path) if os.path.dirname(dest_path) else backup_path, exist_ok=True)
                shutil.copy2(file_path, dest_path)

        # 生成审查报告
        report_file = os.path.join(backup_path, "review_report.json")
        report = {
            "timestamp": timestamp,
            "base_commit": base_commit,
            "rejected_files": [
                {
                    "file_path": d.file_path,
                    "verdict": d.verdict.value,
                    "confidence": d.confidence,
                    "issues": d.issues,
                    "suggestions": d.suggestions,
                    "requires_human": d.requires_human,
                    "reject_reason": self._get_reject_reason(d)
                }
                for d in blocked_decisions
            ]
        }
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        # 执行 git 回滚
        stash_msg = f"gemini_reject_snapshot_{timestamp}"
        subprocess.run(
            ["git", "stash", "push", "-u", "-m", stash_msg, "--"] + reject_files,
            capture_output=True
        )

        for file_path in reject_files:
            subprocess.run(
                ["git", "checkout", "HEAD", "--", file_path],
                capture_output=True
            )

        # 生成打回原因摘要
        reject_reasons = list(set(self._get_reject_reason(d) for d in blocked_decisions))

        print(f"[REJECT] Gemini review blocked {len(blocked_decisions)} file(s)")
        print(f"[REJECT] Snapshot saved to: {backup_path}")
        print(f"[REJECT] Reasons: {'; '.join(reject_reasons)}")

        return True, "; ".join(reject_reasons)

    def _get_reject_reason(self, decision: ReviewDecision) -> str:
        """获取打回原因代码"""
        if decision.error_code:
            return "API_FAILURE"

        if decision.requires_human and not decision.blocked:
            return "HUMAN_REVIEW_REQUIRED"

        # 分析问题类型
        for issue in decision.issues:
            issue_lower = issue.lower()
            if any(kw in issue_lower for kw in ["security", "injection", "xss", "command"]):
                return "SECURITY_VIOLATION"
            if any(kw in issue_lower for kw in ["signature", "interface", "parameter"]):
                return "INTERFACE_DRIFT"
            if any(kw in issue_lower for kw in ["exception", "try", "catch"]):
                return "EXCEPTION_REMOVAL"
            if any(kw in issue_lower for kw in ["dangerous", "eval", "exec", "pickle"]):
                return "DANGEROUS_PATTERN"

        if decision.confidence < 0.4:
            return "LOW_CONFIDENCE"

        return "HUMAN_REVIEW_REQUIRED"

    def generate_report(self, result: BatchReviewResult) -> str:
        """
        生成人类可读的审查报告

        参数：
            result: 批量审查结果

        返回：
            格式化的报告字符串
        """
        lines = [
            "=" * 70,
            "GEMINI AI 代码审查报告",
            "=" * 70,
            f"时间：{result.timestamp}",
            f"总计：{result.total_files} 文件",
            f"状态：{result.summary}",
            "-" * 70
        ]

        for decision in result.decisions:
            status_icon = "[PASS]" if decision.passed else "[BLOCK]" if decision.blocked else "[REVIEW]"
            lines.append(f"\n{status_icon} {decision.file_path}")
            lines.append(f"    裁决：{decision.verdict.value}")
            lines.append(f"    置信度：{decision.confidence:.2f}")
            lines.append(f"    迭代次数：{decision.iterations}")

            if decision.issues:
                lines.append("    问题:")
                for i, issue in enumerate(decision.issues, 1):
                    lines.append(f"      {i}. {issue}")

            if decision.suggestions:
                lines.append("    建议:")
                for i, suggestion in enumerate(decision.suggestions, 1):
                    lines.append(f"      {i}. {suggestion}")

        lines.append("\n" + "=" * 70)

        return "\n".join(lines)


def create_review_manager(config: Dict[str, Any] = None) -> ReviewManager:
    """
    工厂函数：创建审查管理器

    参数：
        config: 配置字典

    返回：
        ReviewManager 实例
    """
    return ReviewManager(config)


# CLI 入口点
def main():
    import argparse

    parser = argparse.ArgumentParser(description="审查管理器 - Gemini AI 代码审查")
    parser.add_argument("files", nargs="+", help="要审查的文件列表")
    parser.add_argument("--ref", default="HEAD", help="Git ref 作为比较基准")
    parser.add_argument("--max-retries", type=int, default=3, help="API 失败时最大重试次数")
    parser.add_argument("--max-iterations", type=int, default=3, help="Reflect-Refine 最大迭代次数")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    parser.add_argument("--auto-reject", action="store_true", help="自动执行打回操作")

    args = parser.parse_args()

    # 创建管理器
    manager = create_review_manager()

    if not manager.evaluator.api_key:
        print("[ERROR] 未找到 API Key，请设置 GEMINI_API_KEY 环境变量")
        sys.exit(1)

    # 执行审查
    print(f"[MANAGER] Starting review of {len(args.files)} file(s)...")
    result = manager.review_files(
        file_paths=args.files,
        ref=args.ref,
        max_retries=args.max_retries,
        max_iterations=args.max_iterations
    )

    # 输出结果
    if args.json:
        output = {
            "timestamp": result.timestamp,
            "summary": result.summary,
            "decisions": [
                {
                    "file_path": d.file_path,
                    "verdict": d.verdict.value,
                    "passed": d.passed,
                    "blocked": d.blocked,
                    "requires_human": d.requires_human,
                    "issues": d.issues,
                    "confidence": d.confidence
                }
                for d in result.decisions
            ]
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(manager.generate_report(result))

    # 自动打回
    if args.auto_reject:
        blocked = [d for d in result.decisions if d.blocked or d.requires_human]
        if blocked:
            # 获取 base commit
            result_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            base_commit = result_proc.stdout.strip()

            executed, reason = manager.execute_rejection(
                decisions=blocked,
                base_commit=base_commit
            )

            if executed:
                print(f"\n[REJECT] 已执行打回：{reason}")
                sys.exit(1)

    # 根据审查结果设置退出码
    blocked = [d for d in result.decisions if d.blocked]
    human = [d for d in result.decisions if d.requires_human and not d.blocked]

    if blocked:
        sys.exit(1)  # 被拦截
    elif human:
        sys.exit(10)  # 需要人工审查
    else:
        sys.exit(0)  # 通过


if __name__ == "__main__":
    main()
