"""cleanup 安全合同回归；只枚举临时 fixture 的文件名，不读取凭据内容。"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILLS = [ROOT / directory / "cleanup-worktrees/SKILL.md" for directory in (
    ".agents/skills", ".claude/skills",
)]


class CleanupSafetyTests(unittest.TestCase):
    def test_contract_uses_complete_nul_paths_and_fails_closed(self):
        for path in SKILLS:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("git ls-files --others --ignored --exclude-standard -z", text)
                self.assertIn("NUL", text)
                self.assertIn("ignored-enumeration-failed", text)
                self.assertIn("ignored-enumeration-incomplete", text)

    def test_nested_repository_is_returned_as_directory_not_leaf_paths(self):
        with tempfile.TemporaryDirectory(prefix="gov-cleanup-nested-") as temp:
            root = Path(temp)
            nested = root / "runtime/nested"
            nested.mkdir(parents=True)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            for repo in (root, nested):
                subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo,
                               env=env, check=True, capture_output=True)
            (root / ".gitignore").write_text("runtime/\n", encoding="utf-8")
            (nested / "auth.json").touch()  # 空占位；测试不读取内容。
            names = subprocess.run(
                ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                cwd=root, env=env, check=True, capture_output=True).stdout
            self.assertEqual(names, b"runtime/nested/\0")

    def test_ignored_directory_summary_hides_leaf_but_nul_enumeration_does_not(self):
        with tempfile.TemporaryDirectory(prefix="gov-cleanup-safety-") as temp:
            root = Path(temp)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, env=env,
                                      check=True, capture_output=True).stdout
            git("init", "-q", "-b", "main")
            (root / ".gitignore").write_text("runtime/\n", encoding="utf-8")
            names = ["runtime/deep/auth.json", "runtime/with space/.env.test",
                     "runtime/普通目录/cookies.json", "runtime/cache.txt"]
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()  # 空文件名 fixture，不创建真实凭据。
            summary = git("status", "--short", "--untracked-files=all", "--ignored=matching")
            self.assertIn(b"!! runtime/", summary)
            self.assertNotIn(b"auth.json", summary)
            entries = git("ls-files", "--others", "--ignored", "--exclude-standard", "-z")
            self.assertEqual(set(entries.decode("utf-8").split("\0")[:-1]), set(names))

    def test_two_skill_bodies_remain_synchronized(self):
        def body(path):
            return [line for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.startswith("description:") and "对话入口" not in line]
        self.assertEqual(body(SKILLS[0]), body(SKILLS[1]))

    def test_claude_entry_matches_current_plan_and_command_surfaces(self):
        text = (ROOT / ".claude/README.md").read_text(encoding="utf-8")
        self.assertIn("~/.claude/plans/*.md", text)
        self.assertIn("需求基线获批后", text)
        self.assertIn("docs/当前规则/40-Agent与Skill.md", text)
        self.assertNotIn("Plan 阶段只允许只读探查和对话内计划", text)
        self.assertNotIn("`review`", text)
        self.assertNotIn("`review-all`", text)
        self.assertEqual({p.stem for p in (ROOT / ".claude/commands").glob("*.md")},
                         {"cleanup-worktrees"})


if __name__ == "__main__":
    unittest.main()
