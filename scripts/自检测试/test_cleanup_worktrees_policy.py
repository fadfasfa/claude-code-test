from __future__ import annotations

"""cleanup-worktrees 规则回归测试。

临时 Git fixture 覆盖普通合并、squash merge、硬干净状态和 expected-old-OID
删除语义；文本测试只锁定两侧 Skill 必须共同保留的安全不变量。
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

# PR 修复边界的唯一事实源是 20-Git与高危操作.md；AGENTS.md 只保留触发条件与指针，
# 不再复述 headRefName / FETCH_HEAD 等细则，因此不在锚定范围内。
PR_POLICY_FILES = (
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


def create_squash_merged_worktree(root: Path) -> tuple[Path, Path, str, str]:
    repo, worktree = create_repo_with_feature_worktree(root)
    write_text(worktree / "feature.txt", "first\n")
    run_git(worktree, "add", "feature.txt")
    run_git(worktree, "commit", "-q", "-m", "feature one")
    write_text(worktree / "feature.txt", "first\nsecond\n")
    run_git(worktree, "commit", "-q", "-am", "feature two")
    candidate_oid = run_git(worktree, "rev-parse", "HEAD").stdout.strip()

    run_git(repo, "merge", "--squash", "codex/fix/pycache-only")
    run_git(repo, "commit", "-q", "-m", "squash feature")
    merge_oid = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, worktree, candidate_oid, merge_oid


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
    def test_skill_text_keeps_squash_and_cleanup_safety_invariants(self) -> None:
        for path in SKILL_FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("先用 `git status --short --untracked-files=all` 判断硬干净状态", text)
                self.assertIn("普通 runtime/cache/log/data 的 `!!` 输出只报告", text)
                self.assertIn("gh pr list --state merged --head <branch>", text)
                self.assertIn("`headRefOid` 等于固定的 `candidate_oid`", text)
                self.assertIn("`squash-merged-pr`", text)
                self.assertIn("git update-ref -d refs/heads/<branch> <candidate_oid>", text)
                self.assertIn("expected-old-OID 不匹配", text)
                self.assertIn("Skill 不自动丢弃 dirty 内容", text)
                for reason in (
                    "pr-metadata-unavailable",
                    "pr-head-oid-mismatch",
                    "merge-commit-not-in-base",
                    "ambiguous-merged-pr",
                    "ref-changed-before-delete",
                ):
                    self.assertIn(reason, text)

    def test_two_skill_copies_stay_in_sync(self) -> None:
        """CC 与 Codex 各持一份 SKILL.md 双端执行；除 description 与入口声明行外，
        正文必须逐行一致，否则两端清理行为会静默漂移。"""
        def normalized(path: Path) -> list[str]:
            lines = path.read_text(encoding="utf-8").splitlines()
            return [
                line for line in lines
                if not line.startswith("description:") and "对话入口" not in line
            ]

        first, second = (normalized(path) for path in SKILL_FILES)
        self.assertEqual(first, second)

    def test_pr_fix_and_review_must_not_create_replacement_branch(self) -> None:
        for path in PR_POLICY_FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("真实 `headRefName`", text)
                self.assertIn("不得新建替代修复分支", text)
                self.assertIn("FETCH_HEAD", text)
                self.assertIn("pull/<编号>/head:pr-<编号>", text)

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

    def test_squash_merge_is_non_ancestor_but_merge_commit_is_in_base(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
            repo, worktree, candidate_oid, merge_oid = create_squash_merged_worktree(Path(temp))
            try:
                self.assertNotEqual(
                    run_git(repo, "merge-base", "--is-ancestor", candidate_oid, "HEAD", check=False).returncode,
                    0,
                )
                self.assertGreater(branch_ahead_count(repo, "HEAD", candidate_oid), 0)
                self.assertEqual(
                    run_git(repo, "merge-base", "--is-ancestor", merge_oid, "HEAD").returncode,
                    0,
                )
            finally:
                remove_worktree(repo, worktree)

    def test_verified_squash_branch_can_be_deleted_with_expected_old_oid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
            repo, worktree, candidate_oid, _ = create_squash_merged_worktree(Path(temp))
            try:
                self.assertEqual(hard_status_lines(worktree), [])
                run_git(repo, "worktree", "remove", str(worktree))
                run_git(
                    repo,
                    "update-ref",
                    "-d",
                    "refs/heads/codex/fix/pycache-only",
                    candidate_oid,
                )
                self.assertNotEqual(
                    run_git(repo, "show-ref", "--verify", "refs/heads/codex/fix/pycache-only", check=False).returncode,
                    0,
                )
            finally:
                remove_worktree(repo, worktree)

    def test_expected_old_oid_mismatch_preserves_changed_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
            repo, worktree, candidate_oid, merge_oid = create_squash_merged_worktree(Path(temp))
            try:
                run_git(repo, "worktree", "remove", str(worktree))
                run_git(repo, "update-ref", "refs/heads/codex/fix/pycache-only", merge_oid, candidate_oid)
                deleted = run_git(
                    repo,
                    "update-ref",
                    "-d",
                    "refs/heads/codex/fix/pycache-only",
                    candidate_oid,
                    check=False,
                )
                self.assertNotEqual(deleted.returncode, 0)
                self.assertEqual(
                    run_git(repo, "rev-parse", "refs/heads/codex/fix/pycache-only").stdout.strip(),
                    merge_oid,
                )
            finally:
                remove_worktree(repo, worktree)

    def test_commit_after_squash_merge_no_longer_matches_pr_head_oid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cleanup-worktrees-policy-") as temp:
            repo, worktree, pr_head_oid, _ = create_squash_merged_worktree(Path(temp))
            try:
                write_text(worktree / "after-merge.txt", "new work\n")
                run_git(worktree, "add", "after-merge.txt")
                run_git(worktree, "commit", "-q", "-m", "post merge work")
                local_oid = run_git(worktree, "rev-parse", "HEAD").stdout.strip()
                self.assertNotEqual(local_oid, pr_head_oid)
                self.assertGreater(branch_ahead_count(repo, "HEAD", local_oid), 0)
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
