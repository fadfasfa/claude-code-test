"""中文简介：repo-local self-improvement 只读检查工具。

什么时候读：需要确认本仓 learning/error/hook wiring 或准备 self-improvement 晋升时读取。
约束什么：只汇总 repo-local settings、hook、learning 文件和最近错误条目。
不负责什么：不修改规则、不写 global/kb、不自动晋升 learning、不修复错误。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[2]
SETTINGS_PATHS = (
    REPO_ROOT / ".claude" / "settings.json",
    REPO_ROOT / ".claude" / "settings.local.json",
)
REPO_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
ERROR_LOG_HOOK_PATH = REPO_HOOKS_DIR / "self-improvement-error-log.sh"
LEARNINGS_DIR = REPO_ROOT / ".learnings"
LEARNINGS_PATH = LEARNINGS_DIR / "LEARNINGS.md"
ERRORS_PATH = LEARNINGS_DIR / "ERRORS.md"
FEATURE_REQUESTS_PATH = LEARNINGS_DIR / "FEATURE_REQUESTS.md"
ERROR_ID_PATTERN = re.compile(r"^##\s+\[?(ERR-\d{8}-\d+)\]?", re.MULTILINE)
CAPTURED_AT_PATTERN = re.compile(r"^-\s+captured_at:\s+`([^`]+)`", re.MULTILINE)
LOGGED_AT_PATTERN = re.compile(r"^\*\*Logged\*\*:\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class ErrorEntry:
    """单条 raw error 的摘要，供报告展示最近错误使用。"""

    entry_id: str
    timestamp: str


def load_settings() -> list[dict[str, Any]]:
    """读取 repo-local settings 文件；不存在的 settings.local.json 会被跳过。"""
    settings: list[dict[str, Any]] = []
    for settings_path in SETTINGS_PATHS:
        if settings_path.exists():
            settings.append(json.loads(settings_path.read_text(encoding="utf-8")))
    return settings


def iter_hook_commands(settings_items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """从 Claude Code settings 中提取 hook event 和 command，不执行命令。"""
    commands: list[tuple[str, str]] = []
    for settings in settings_items:
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event_name, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                nested_hooks = entry.get("hooks")
                if not isinstance(nested_hooks, list):
                    continue
                for hook in nested_hooks:
                    if not isinstance(hook, dict):
                        continue
                    command = hook.get("command")
                    if isinstance(command, str) and command:
                        commands.append((event_name, command))
    return commands


def has_visible_skill(commands: list[tuple[str, str]], skill_name: str) -> bool:
    """检查 hook command 文本中是否出现某个 skill 路径，仅用于可见性提示。"""
    needle = f"/{skill_name}/"
    return any(needle in command.replace("\\", "/") for _, command in commands)


def is_repo_local_path(path_text: str) -> bool:
    """判断 hook command 是否指向本仓 .claude/hooks，避免误把全局 hook 当成本仓 hook。"""
    normalized = path_text.replace("\\", "/").lower()
    repo_hooks_path = REPO_HOOKS_DIR.as_posix().lower()
    return normalized.startswith(repo_hooks_path) or f" {repo_hooks_path}" in normalized or repo_hooks_path + "/" in normalized


def repo_relative(path: Path) -> str:
    """尽量把路径转成仓库相对路径，失败时保留原路径文本。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def describe_path(path: Path) -> str:
    """同时输出仓库相对路径和绝对路径，方便人工核对 wiring。"""
    return f"{repo_relative(path)} ({path.resolve().as_posix()})"


def parse_error_entries() -> list[ErrorEntry]:
    """解析 ignored ERRORS.md 的错误条目摘要；不修改 raw log。"""
    if not ERRORS_PATH.exists():
        return []

    text = ERRORS_PATH.read_text(encoding="utf-8")
    entries: list[ErrorEntry] = []
    matches = list(ERROR_ID_PATTERN.finditer(text))
    for index, match in enumerate(matches):
        entry_id = match.group(1)
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[start:end]
        captured_match = CAPTURED_AT_PATTERN.search(chunk)
        logged_match = LOGGED_AT_PATTERN.search(chunk)
        timestamp = "unknown"
        if captured_match:
            timestamp = captured_match.group(1).strip()
        elif logged_match:
            timestamp = logged_match.group(1).strip()
        entries.append(ErrorEntry(entry_id=entry_id, timestamp=timestamp))
    return entries


def print_hook_summary(commands: list[tuple[str, str]]) -> None:
    """打印 settings 中当前可见的 hook 命令。"""
    print("当前生效 hook 文件位置:")
    if not commands:
        print("- 未找到 hook 配置")
        return
    for event_name, command in commands:
        print(f"- {event_name}: {command}")


def print_skill_visibility(commands: list[tuple[str, str]]) -> None:
    """打印历史 self-improvement 相关 skill 是否通过 hook command 暴露。"""
    print("skill 可见性:")
    for skill_name in ("self-improvement", "pre-flight-check", "context-surfing"):
        visible = "yes" if has_visible_skill(commands, skill_name) else "no"
        print(f"- {skill_name}: {visible}")


def print_learning_files() -> None:
    """打印 repo-local learning/error 文件存在性。"""
    print("repo-local 文件存在性:")
    for path in (*SETTINGS_PATHS, ERROR_LOG_HOOK_PATH, LEARNINGS_PATH, ERRORS_PATH, FEATURE_REQUESTS_PATH):
        exists = "yes" if path.exists() else "no"
        print(f"- {path.name}: {exists} ({describe_path(path)})")


def print_error_summary(entries: list[ErrorEntry]) -> None:
    """打印最近 raw error 摘要，避免输出完整错误体。"""
    print(f"ERRORS.md 条目数: {len(entries)}")
    print("最近 3 条错误条目:")
    if not entries:
        print("- 无")
        return
    for entry in entries[-3:]:
        print(f"- {entry.entry_id} | {entry.timestamp}")


def print_repo_local_hook_status(commands: list[tuple[str, str]]) -> None:
    """报告当前 settings 是否实际使用 repo-local hook。"""
    repo_local_commands = [command for _, command in commands if is_repo_local_path(command)]
    print(f"当前是否使用 repo-local hook: {'yes' if repo_local_commands else 'no'}")
    for command in repo_local_commands:
        print(f"- repo-local: {command}")


def main() -> None:
    """只读检查入口：收集 settings、hook、learning 和 raw error 摘要后打印报告。"""
    settings = load_settings()
    commands = iter_hook_commands(settings)
    entries = parse_error_entries()

    print(f"repo root: {REPO_ROOT.as_posix()}")
    print("检查配置文件:")
    for settings_path in SETTINGS_PATHS:
        print(f"- {describe_path(settings_path)}")
    print_hook_summary(commands)
    print_skill_visibility(commands)
    print_learning_files()
    print_error_summary(entries)
    print_repo_local_hook_status(commands)


if __name__ == "__main__":
    main()
