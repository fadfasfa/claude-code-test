"""直接向 hook stdin 传 fixture refs；不安装 hook，不运行 git push。"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".githooks/pre-push"
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash")
ZERO = "0" * 40


@unittest.skipUnless(BASH, "需要 Git Bash")
class PrePushGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gov-pre-push-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "GOV fixture")
        self.git("config", "user.email", "gov@example.invalid")
        self.git("config", "core.hooksPath", str(self.repo / "no-hooks"))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()

    def commit(self, filename, content="fixture"):
        path = self.repo / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", "--", filename)
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def check_hook(self, new, old=ZERO, *, diff_failure=False):
        command = [BASH, HOOK.as_posix()]
        if diff_failure:
            command = [BASH, "-c", 'git() { if [ "$1" = diff-tree ]; then return 128; fi; command git "$@"; }; export -f git; exec bash "$1"',
                       "fixture", HOOK.as_posix()]
        return subprocess.run(command, cwd=self.repo, env=self.env,
                              # Git hook 协议使用 LF；Windows text=True 会把它改成 CRLF。
                              input=f"refs/heads/main {new} refs/heads/main {old}\n".encode(),
                              capture_output=True)

    def test_new_branch_checks_earlier_commits(self):
        self.commit("safe.txt")
        self.commit("QuantProject/private.txt")
        tip = self.commit("later.txt")
        self.assertNotEqual(self.check_hook(tip).returncode, 0)

    def test_add_then_delete_still_blocks_history(self):
        base = self.commit("safe.txt")
        self.commit("QuantProject/private.txt")
        self.git("rm", "QuantProject/private.txt")
        self.git("commit", "-qm", "remove")
        self.assertNotEqual(self.check_hook(self.git("rev-parse", "HEAD"), base).returncode, 0)

    def test_private_root_commit_blocks(self):
        self.assertNotEqual(self.check_hook(self.commit("QuantProject/private.txt")).returncode, 0)

    def test_unrelated_new_branch_allows(self):
        self.assertEqual(self.check_hook(self.commit("safe.txt")).returncode, 0)

    def test_deletion_only_update_allows(self):
        base = self.commit("QuantProject/private.txt")
        self.git("rm", "QuantProject/private.txt")
        self.git("commit", "-qm", "remove")
        self.assertEqual(self.check_hook(self.git("rev-parse", "HEAD"), base).returncode, 0)

    def test_deleted_remote_ref_allows(self):
        base = self.commit("safe.txt")
        self.assertEqual(self.check_hook(ZERO, base).returncode, 0)

    def test_invalid_commit_fails_closed(self):
        self.commit("safe.txt")
        self.assertNotEqual(self.check_hook("f" * 40).returncode, 0)

    def test_diff_failure_fails_closed(self):
        tip = self.commit("safe.txt")
        self.assertNotEqual(self.check_hook(tip, diff_failure=True).returncode, 0)

    def test_merge_resolution_private_change_blocks(self):
        base = self.commit("safe.txt")
        self.git("checkout", "-qb", "side")
        self.commit("side.txt")
        self.git("checkout", "-q", "main")
        self.commit("main.txt")
        self.git("merge", "--no-commit", "--no-ff", "side")
        tip = self.commit("QuantProject/合并内容 with space.txt")
        self.assertNotEqual(self.check_hook(tip, base).returncode, 0)


if __name__ == "__main__":
    unittest.main()
