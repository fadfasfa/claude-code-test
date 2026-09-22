"""legacy Git 辅助脚本回归。所有 Git 写操作仅发生于单个系统临时目录。"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
GC = ROOT / "scripts/Git辅助/ccw-gc.ps1"
LS = ROOT / "scripts/Git辅助/ccw-ls.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "需要 PowerShell")
class LegacyGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gov-legacy-git-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.managed = self.root / "managed"
        self.repo.mkdir()
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "GOV fixture")
        self.git("config", "user.email", "gov@example.invalid")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        (self.repo / ".gitignore").write_text(
            "runtime/\nsms-monitor/config.json*\nsms-monitor/private-import.txt*\n",
            encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", check=True)

    def worktree(self, branch="feature", name="candidate", *, managed=True):
        path = (self.managed if managed else self.root / "outside") / name
        self.git("worktree", "add", "-q", "-b", branch, str(path), "main")
        return path

    def collect(self):
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(GC),
                               "-Apply", "-Root", str(self.managed), "-Base", "main"],
                              cwd=self.repo, env=self.env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def assert_preserved(self, path):
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(path.is_dir(), result.stdout + result.stderr)

    def test_nested_sensitive_ignored_file_blocks_removal(self):
        path = self.worktree()
        placeholder = path / "runtime" / "普通目录" / "auth.json"
        placeholder.parent.mkdir(parents=True)
        placeholder.touch()
        self.assert_preserved(path)

    def test_ignored_nested_repository_blocks_removal_without_recursing(self):
        path = self.worktree()
        nested = path / "runtime/nested"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=nested,
                       env=self.env, check=True, capture_output=True)
        (nested / "auth.json").touch()
        names = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            cwd=path, env=self.env, check=True, capture_output=True).stdout
        self.assertEqual(names, b"runtime/nested/\0")
        result = self.collect()
        self.assertTrue(path.is_dir(), result.stdout + result.stderr)
        self.assertTrue((nested / "auth.json").is_file())
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ignored-enumeration-incomplete", result.stdout + result.stderr)
        self.assertTrue(self.git("show-ref", "--verify", "refs/heads/feature").stdout)

    def test_protected_branch_blocks_removal(self):
        self.assert_preserved(self.worktree(branch="develop"))

    def test_sensitive_path_variants_block_without_reading_contents(self):
        names = ["runtime/.env.test", "runtime/child/.credentials.json",
                 "runtime/cookies.sqlite", "runtime/token.json", "runtime/api_key.txt",
                 "runtime/storageState.json", "runtime/.git-credentials",
                 "sms-monitor/config.json.backup", "sms-monitor/private-import.txt.tmp"]
        paths = []
        for index, name in enumerate(names):
            path = self.worktree(branch=f"feature-{index}", name=f"candidate-{index}")
            placeholder = path / name
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            placeholder.touch()
            paths.append(path)
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, path in zip(names, paths):
            with self.subTest(name=name):
                self.assertTrue(path.is_dir(), result.stdout + result.stderr)

    def test_registered_private_branch_blocks_removal(self):
        self.assert_preserved(self.worktree(branch="codex/feat/xiangqi-local"))

    def test_locked_worktree_is_skipped(self):
        path = self.worktree()
        self.git("worktree", "lock", str(path))
        self.assert_preserved(path)

    def test_normal_ignored_cache_does_not_block(self):
        path = self.worktree()
        (path / "runtime").mkdir()
        (path / "runtime/cache.txt").touch()
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(path.exists())

    def test_cleanup_does_not_prune_unrelated_metadata(self):
        self.worktree()
        unrelated = self.worktree(branch="other", name="unrelated", managed=False)
        metadata = self.repo / ".git/worktrees/unrelated"
        self.assertTrue(metadata.is_dir())
        # 显式验证删除目标位于本测试临时目录；不涉及任何真实工作树。
        self.assertTrue(unrelated.resolve().is_relative_to(self.root))
        shutil.rmtree(unrelated)
        self.git("config", "gc.worktreePruneExpire", "now")
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(metadata.is_dir(), result.stdout + result.stderr)

    def run_function(self, script, function, invocation, mock=""):
        # 只提取指定函数，绝不 dot-source 脚本主流程。
        quoted = str(script).replace("'", "''")
        code = f"""
$ErrorActionPreference='Stop'
$tree=[System.Management.Automation.Language.Parser]::ParseFile('{quoted}',[ref]$null,[ref]$null)
$node=$tree.Find({{param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq '{function}'}},$true)
if ($null -eq $node) {{ throw 'missing function' }}
. ([scriptblock]::Create($node.Extent.Text))
{mock}
try {{ {invocation} | Out-Null; Write-Output 'UNSAFE-SUCCESS' }} catch {{ Write-Output 'BLOCKED' }}
"""
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", code],
                              env=self.env, cwd=self.repo, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def test_failed_status_never_becomes_clean(self):
        for script, function in ((GC, "Test-Clean"), (LS, "Get-DirtyFlag")):
            with self.subTest(script=script):
                result = self.run_function(script, function, f"{function} -Path 'unused'",
                                          "function git { $global:LASTEXITCODE=128 }")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "BLOCKED")

    def test_failed_worktree_query_is_not_empty_success(self):
        for script in (GC, LS):
            with self.subTest(script=script):
                result = self.run_function(script, "Parse-WorktreeList",
                                          "Parse-WorktreeList -RepoRoot 'unused'",
                                          "function git { $global:LASTEXITCODE=128 }")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "BLOCKED")

    def test_failed_ignored_enumeration_blocks(self):
        result = self.run_function(GC, "Test-SensitiveIgnored",
                                  f"Test-SensitiveIgnored -Path '{self.root}'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "BLOCKED")

    def test_nonleaf_or_unavailable_metadata_blocks_even_without_trailing_slash(self):
        path = self.worktree()
        (path / "runtime").mkdir()
        (path / "runtime/cache.txt").touch()
        mocks = (
            "function Get-Item { [pscustomobject]@{PSIsContainer=$true; Attributes=[IO.FileAttributes]::Directory} }",
            "function Get-Item { [pscustomobject]@{PSIsContainer=$false; Attributes=[IO.FileAttributes]::ReparsePoint} }",
            "function Get-Item { throw 'fixture metadata unavailable' }",
        )
        for mock in mocks:
            with self.subTest(mock=mock):
                result = self.run_function(GC, "Test-SensitiveIgnored",
                                          f"Test-SensitiveIgnored -Path '{path}'", mock)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "BLOCKED")
                self.assertTrue(path.is_dir())

    def test_failed_merge_query_blocks(self):
        result = self.run_function(GC, "Get-MergeState",
                                  "Get-MergeState -RepoRoot 'unused' -Branch 'feature' -Base 'main'",
                                  "function git { $global:LASTEXITCODE=128 }")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "BLOCKED")


if __name__ == "__main__":
    unittest.main()
