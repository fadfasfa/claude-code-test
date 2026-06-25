from __future__ import annotations

"""cleanup-worktrees 规则回归测试。

本文件用临时 Git fixture 验证 cleanup 以无 ignored 的 status 判断硬干净状态，
普通 ignored 产物只报告、不阻断已合并 stale worktree 的整体移除，
同时检查 Codex / Claude 两侧 skill 文本不会退回“所有 ignored 都阻断”的旧口径。
"""

import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

SKILL_FILES = (
    REPO_ROOT / ".agents" / "skills" / "cleanup-worktrees" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "cleanup-worktrees" / "SKILL.md",
)

POLICY_FILES = SKILL_FILES + (
    REPO_ROOT / ".agents" / "skills" / "README.md",
    REPO_ROOT / ".claude" / "commands" / "cleanup-worktrees.md",
    REPO_ROOT / ".claude" / "README.md",
    REPO_ROOT / "docs" / "当前规则" / "40-Agent与Skill.md",
    REPO_ROOT / "docs" / "当前规则" / "20-Git与高危操作.md",
)


def run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def create_repo_with_feature_worktree(root: Path) -> tuple[Path, Path]:
    repo = root / "repo"
    worktree = root / "managed" / "repo-codex-fix-pycache"
    repo.mkdir(parents=True)

    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Cleanup Worktrees Test")
    write_text(repo / ".gitignore", "__pycache__/\nbuild/\ndist/\nnode_modules/\nrun/data/runtime/\n.env\n")
    write_text(repo / "tracked.txt", "base\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "init")
    run_git(repo, "branch", "codex/fix/pycache-only")
    run_git(repo, "worktree", "add", "-q", str(worktree), "codex/fix/pycache-only")
    return repo, worktree


def ignored_status_lines(worktree: Path) -> list[str]:
    result = run_git(worktree, "status", "--short", "--untracked-files=all", "--ignored=matching")
    return [line for line in result.stdout.splitlines() if line.strip()]


def hard_status_lines(worktree: Path) -> list[str]:
    result = run_git(worktree, "status", "--short", "--untracked-files=all")
    return [line for line in result.stdout.splitlines() if line.strip()]


def has_sensitive_ignored_line(status_lines: list[str]) -> bool:
    sensitive_markers = (".env", "auth.json", "local.yaml", "proxies.json", "accounts.json")
    for line in status_lines:
        if not line.startswith("!! "):
            continue
        path = line[3:].strip().strip('"').replace("\\", "/").lower()
        if any(marker in path for marker in sensitive_markers):
            return True
    return False


def branch_ahead_count(repo: Path, base: str, branch: str) -> int:
    counts = run_git(repo, "rev-list", "--left-right", "--count", f"{base}...{branch}").stdout.split()
    return int(counts[1])


def remove_worktree(repo: Path, worktree: Path) -> None:
    if not worktree.exists():
        return
    run_git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    shutil.rmtree(worktree, ignore_errors=True)


class CleanupWorktreesPolicyTextTests(unittest.TestCase):
    def test_policy_text_treats_ignored_as_report_only_and_uses_chinese_tables(self) -> None:
        for path in SKILL_FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("## Ignored 内容处理", text)
                self.assertIn("先用 `git status --short --untracked-files=all` 判断硬干净状态", text)
                self.assertIn("`!!` ignored 输出不再单独阻断", text)
                self.assertIn("transient review worktree root", text)
                self.assertIn("`pr-[0-9]+`", text)
                self.assertIn("`worktree-agent-*`", text)
                self.assertIn("**工作树**：`路径`、`分支`、`结论`、`原因`", text)
                self.assertIn("**本地分支**：`分支`、`已合入 base`、`领先提交数`、`结论`、`原因`", text)
                self.assertIn("**远端跟踪缓存**：`ref`、`结论`、`原因`", text)

    def test_policy_text_drops_old_over_strict_or_english_output_wording(self) -> None:
        forbidden_phrases = (
            "任何输出都视为本地内容未清空",
            "无 untracked/ignored 本地文件",
            "含 ignored 缓存",
            "白名单 ignored 缓存",
            "仅有 `!!` ignored 输出且全部匹配任意层级 `__pycache__/`",
            "非白名单 ignored 文件或目录",
            "orphan branches",
            "`decision`、`reason`",
        )
        for path in POLICY_FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                for phrase in forbidden_phrases:
                    self.assertNotIn(phrase, text)


class CleanupWorktreesGitFixtureTests(unittest.TestCase):
    def test_ignored_only_status_is_hard_clean_even_with_runtime_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
            repo, worktree = create_repo_with_feature_worktree(Path(temp))
            try:
                write_text(worktree / "build" / "artifact.txt", "local\n")
                write_text(worktree / "run" / "data" / "runtime" / "cache" / "state.json", "{}\n")

                self.assertEqual(hard_status_lines(worktree), [])
                ignored = ignored_status_lines(worktree)
                self.assertTrue(any("build/" in line for line in ignored))
                self.assertTrue(any("run/data/runtime/" in line for line in ignored))
                self.assertFalse(has_sensitive_ignored_line(ignored))
            finally:
                remove_worktree(repo, worktree)

    def test_merged_ahead_zero_worktree_with_ignored_outputs_is_removable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
            repo, worktree = create_repo_with_feature_worktree(Path(temp))
            try:
                write_text(worktree / "sms-monitor" / "__pycache__" / "monitor.cpython-313.pyc", "cache\n")
                write_text(worktree / "build" / "artifact.txt", "local\n")
                write_text(worktree / "run" / "data" / "runtime" / "logs" / "last.log", "log\n")

                self.assertEqual(hard_status_lines(worktree), [])
                self.assertFalse(has_sensitive_ignored_line(ignored_status_lines(worktree)))

                self.assertEqual(
                    run_git(repo, "merge-base", "--is-ancestor", "codex/fix/pycache-only", "HEAD").returncode,
                    0,
                )
                self.assertEqual(branch_ahead_count(repo, "HEAD", "codex/fix/pycache-only"), 0)

                removed = run_git(repo, "worktree", "remove", str(worktree))
                self.assertEqual(removed.returncode, 0)
                self.assertFalse(worktree.exists())
            finally:
                remove_worktree(repo, worktree)

    def test_branch_with_ahead_commit_is_not_candidate_even_with_ignored_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
            repo, worktree = create_repo_with_feature_worktree(Path(temp))
            try:
                write_text(worktree / "feature.txt", "ahead\n")
                run_git(worktree, "add", "feature.txt")
                run_git(worktree, "commit", "-q", "-m", "ahead")
                write_text(worktree / "sms-monitor" / "__pycache__" / "monitor.pyc", "cache\n")

                self.assertEqual(hard_status_lines(worktree), [])
                self.assertGreater(branch_ahead_count(repo, "HEAD", "codex/fix/pycache-only"), 0)
            finally:
                remove_worktree(repo, worktree)

    def test_status_classifier_rejects_tracked_untracked_and_sensitive_ignored_content(self) -> None:
        cases = {
            "tracked modified": (
                lambda wt: write_text(wt / "tracked.txt", "changed\n"),
                lambda wt: bool(hard_status_lines(wt)),
            ),
            "untracked file": (
                lambda wt: write_text(wt / "notes.txt", "local\n"),
                lambda wt: bool(hard_status_lines(wt)),
            ),
            "sensitive ignored": (
                lambda wt: write_text(wt / ".env", "SECRET=redacted\n"),
                lambda wt: has_sensitive_ignored_line(ignored_status_lines(wt)),
            ),
        }

        for name, (setup, is_rejected) in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
                    _, worktree = create_repo_with_feature_worktree(Path(temp))
                    repo = Path(temp) / "repo"
                    try:
                        setup(worktree)
                        self.assertTrue(is_rejected(worktree))
                    finally:
                        remove_worktree(repo, worktree)


if __name__ == "__main__":
    unittest.main()
