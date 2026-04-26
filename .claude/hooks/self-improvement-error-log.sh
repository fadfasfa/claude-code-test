#!/bin/bash
# 中文简介：repo-local Claude Code hook，把失败的 Bash 调用追加到 ./.learnings/ERRORS.md。
# 触发条件：PostToolUseFailure，matcher: Bash。
# 安全边界：只写本仓 ignored raw error cache 和本地 debug log；不修复错误、不调度任务、不修改 global/kb。

set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd -P)"
if [ -z "$PROJECT_DIR" ]; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
fi
DEBUG_DIR="$PROJECT_DIR/.claude/hooks/.debug"
DEBUG_LOG="$DEBUG_DIR/self-improvement-error-log.ndjson"
PAYLOAD_FILE="$(mktemp 2>/dev/null)"
if [ -z "$PAYLOAD_FILE" ]; then
  mkdir -p "$DEBUG_DIR" 2>/dev/null
  printf '{"timestamp":"%s","level":"error","message":"mktemp_failed","pwd":"%s","CLAUDE_PROJECT_DIR":"%s"}\n' "$(date -Iseconds 2>/dev/null)" "$PWD" "$CLAUDE_PROJECT_DIR" >> "$DEBUG_LOG" 2>/dev/null
  exit 0
fi
trap 'rm -f "$PAYLOAD_FILE"' EXIT
cat > "$PAYLOAD_FILE"

if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v py >/dev/null 2>&1; then
  PYTHON_BIN="py"
else
  mkdir -p "$DEBUG_DIR" 2>/dev/null
  printf '{"timestamp":"%s","level":"error","message":"python_not_found","pwd":"%s","CLAUDE_PROJECT_DIR":"%s"}\n' "$(date -Iseconds 2>/dev/null)" "$PWD" "$CLAUDE_PROJECT_DIR" >> "$DEBUG_LOG" 2>/dev/null
  exit 0
fi

"$PYTHON_BIN" - "$PROJECT_DIR" "$PAYLOAD_FILE" "$DEBUG_LOG" <<'PY'
# 中文简介：解析 Claude Code failure payload，去重并截断写入 repo-local raw error cache。
# 输入：project_dir、payload_file、debug_log。
# 输出：必要时更新 .learnings/ERRORS.md；任何内部异常只写 debug log 并返回 0。
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import traceback

MAX_BYTES = 65536
MAX_ENTRIES = 50
HEADER = "# Errors\n\nCommand failures and integration errors.\n\n---\n"
TRUNCATION_MARKER = "<!-- truncated by self-improvement-error-log.sh -->"
ENTRY_PATTERN = re.compile(r"(?ms)^## \[(ERR-[^\]]+)\]\n.*?(?=^## \[ERR-|\Z)")


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _inline(value):
    text = _as_text(value).replace("`", "\\`")
    return text.replace("\r", " ").replace("\n", " ")


def _fence(value):
    text = _as_text(value)
    return text.replace("```", "` ` `")


def _nested(mapping, *keys):
    cur = mapping
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _debug(debug_log, **record):
    """向 ignored debug log 写诊断；失败时静默，避免 hook 阻塞主流程。"""
    try:
        os.makedirs(os.path.dirname(debug_log), exist_ok=True)
        record.setdefault("timestamp", _dt.datetime.now().astimezone().isoformat(timespec="seconds"))
        with open(debug_log, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _normalize_command(command):
    return " ".join(_as_text(command).split())


def _first_error_line(error_text):
    for line in _as_text(error_text).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:500]
    return ""


def _entry_hashes(entry):
    return set(re.findall(r"payload_hash: `([^`]+)`", entry))


def _entry_field(entry, field):
    match = re.search(rf"(?m)^- {re.escape(field)}: `(.*)`$", entry)
    return match.group(1) if match else ""


def _entry_error_first_line(entry):
    match = re.search(r"(?ms)^- error:\n```text\n(.*?)\n```", entry)
    if not match:
        return ""
    return _first_error_line(match.group(1))


def _dedupe_key(cwd, command, error_text):
    """基于 cwd、规范化命令和首条错误行生成稳定去重键。"""
    material = {
        "cwd": _as_text(cwd),
        "command": _normalize_command(command),
        "error_first_line": _first_error_line(error_text),
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _entry_dedupe_keys(entry):
    keys = set(re.findall(r"dedupe_key: `([^`]+)`", entry))
    if keys:
        return keys
    cwd = _entry_field(entry, "cwd")
    command = _entry_field(entry, "command").replace("\\`", "`")
    error_first_line = _entry_error_first_line(entry)
    if not (cwd or command or error_first_line):
        return set()
    return {_dedupe_key(cwd, command, error_first_line)}


def _read_errors(errors_path):
    if not os.path.exists(errors_path):
        return HEADER
    with open(errors_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _extract_entries(content):
    return [match.group(0).rstrip() + "\n" for match in ENTRY_PATTERN.finditer(content)]


def _compose_content(entries, was_truncated):
    parts = [HEADER.rstrip(), ""]
    if was_truncated:
        parts.extend([TRUNCATION_MARKER, ""])
    parts.extend(entry.rstrip() for entry in entries[-MAX_ENTRIES:])
    return "\n\n".join(part for part in parts if part).rstrip() + "\n"


def _trim_to_limits(entries):
    kept = entries[-MAX_ENTRIES:]
    truncated = len(entries) > len(kept)
    content = _compose_content(kept, truncated)
    while len(content.encode("utf-8")) > MAX_BYTES and len(kept) > 1:
        kept = kept[1:]
        truncated = True
        content = _compose_content(kept, truncated)
    return content, truncated


def _write_errors(errors_path, entries):
    """将错误条目写回 ERRORS.md，并强制执行条目数和文件大小上限。"""
    content, _ = _trim_to_limits(entries)
    with open(errors_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def main():
    """主入口：只处理 Bash 的 PostToolUseFailure，并把失败信息落到本仓 raw cache。"""
    project_dir = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
    payload_path = sys.argv[2]
    debug_log = sys.argv[3]
    try:
        with open(payload_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        _debug(debug_log, level="error", message="payload_parse_failed", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        return 0

    event_name = data.get("hook_event_name") or data.get("event_name") or data.get("event")
    tool_name = data.get("tool_name") or _nested(data, "tool", "name")
    if event_name != "PostToolUseFailure" or tool_name != "Bash":
        return 0

    tool_input = data.get("tool_input") or data.get("input") or _nested(data, "tool", "input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    command = tool_input.get("command") or data.get("command") or ""
    description = tool_input.get("description") or data.get("description") or ""
    cwd = data.get("cwd") or tool_input.get("cwd") or project_dir
    error_text = (
        data.get("error")
        or _nested(data, "tool_response", "error")
        or _nested(data, "tool_response", "stderr")
        or _nested(data, "tool_response", "output")
        or data.get("tool_output")
        or data.get("output")
        or data.get("message")
        or ""
    )
    interrupted = data.get("is_interrupt")
    if interrupted is None:
        interrupted = data.get("interrupted")
    if interrupted is None:
        interrupted = data.get("interrupt")
    if interrupted is None:
        interrupted = False

    try:
        cwd_abs = os.path.abspath(_as_text(cwd) or project_dir)
        rel_cwd = os.path.relpath(cwd_abs, project_dir)
        if rel_cwd == ".":
            rel_cwd = "."
        elif rel_cwd.startswith(".."):
            rel_cwd = cwd_abs
    except Exception:
        rel_cwd = _as_text(cwd) or "."

    now = _dt.datetime.now().astimezone()
    captured_at = now.isoformat(timespec="seconds")
    entry_id = "ERR-" + now.strftime("%Y%m%d-%H%M%S")

    hash_material = json.dumps({"event": event_name, "tool": tool_name, "cwd": rel_cwd, "command": command, "description": description, "error": error_text, "interrupted": interrupted}, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(hash_material.encode("utf-8")).hexdigest()[:16]
    dedupe_key = _dedupe_key(rel_cwd, command, error_text)
    learnings_dir = os.path.join(project_dir, ".learnings")
    errors_path = os.path.join(learnings_dir, "ERRORS.md")
    try:
        os.makedirs(learnings_dir, exist_ok=True)
        existing_content = _read_errors(errors_path)
        if re.search(r"(?m)^## \[LRN-", existing_content):
            _debug(debug_log, level="warning", message="errors_log_contains_lrn_sections", errors_path=errors_path)

        entries = _extract_entries(existing_content)
        recent_entries = entries[-MAX_ENTRIES:]
        recent_hashes = set()
        recent_dedupe_keys = set()
        for entry in recent_entries:
            recent_hashes.update(_entry_hashes(entry))
            recent_dedupe_keys.update(_entry_dedupe_keys(entry))

        if payload_hash in recent_hashes or dedupe_key in recent_dedupe_keys:
            _write_errors(errors_path, entries)
            return 0

        entry = f"""
## [{entry_id}]

- event: `{_inline(event_name)}`
- cwd: `{_inline(rel_cwd)}`
- command: `{_inline(command)}`
- description: `{_inline(description)}`
- error:
```text
{_fence(error_text)}
```
- interrupted: `{str(interrupted).lower()}`
- captured_at: `{_inline(captured_at)}`
- payload_hash: `{payload_hash}`
- dedupe_key: `{dedupe_key}`

---
""".lstrip()
        entries.append(entry)
        _write_errors(errors_path, entries)
    except Exception as exc:
        _debug(debug_log, level="error", message="error_log_write_failed", errors_path=errors_path, event_name=event_name, tool_name=tool_name, command=command, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    return 0

try:
    raise SystemExit(main())
except Exception as exc:
    try:
        _debug(sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.getcwd(), ".claude", "hooks", ".debug", "self-improvement-error-log.ndjson"), level="error", message="unexpected_wrapper_failure", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    except Exception:
        pass
    raise SystemExit(0)
PY

exit 0
