"""真实 Hextech 数据链与最终便携包验收器。

本工具只把真实数据、日志和截图写入 worktree 根 ``.artifacts/acceptance``。
默认强制远端刷新，任何 fallback、空数据、混代、空截图或残留进程都会返回非零。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


RUN_DIR = Path(__file__).resolve().parents[2]
WORKTREE_DIR = RUN_DIR.parent
DEFAULT_ARTIFACT_ROOT = WORKTREE_DIR / ".artifacts" / "acceptance"
DATA_SERVICE_NONCE_HEADER = "X-Hextech-Data-Service-Nonce"
if str(RUN_DIR) not in sys.path:
    # 直接执行本文件时 sys.path[0] 是 tools/acceptance，后半段延迟 import
    # 无法发现 run/hextech；验收必须在远端抓取前就固定源码 import 根。
    sys.path.insert(0, str(RUN_DIR))


class AcceptanceFailure(RuntimeError):
    """验收证据不足或任一关键链路失败。"""


@dataclass
class DataServiceProcess:
    process: subprocess.Popen[str]
    port: int
    nonce: str
    stderr_stream: Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceFailure(f"JSON 无法读取：{path}: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceFailure(f"JSON 必须是对象：{path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _child_env(base_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HEXTECH_BASE_DIR"] = str(base_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    current_path = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = str(RUN_DIR) + (os.pathsep + current_path if current_path else "")
    return env


def _prepare_isolated_root(base_dir: Path) -> None:
    if base_dir.exists():
        shutil.rmtree(base_dir)
    source_static = RUN_DIR / "data" / "static"
    shutil.copytree(source_static, base_dir / "data" / "static")
    state_dir = base_dir / "data" / "runtime" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        state_dir / "ui_feature_flags.json",
        {
            "web_frontend_enabled": False,
            "game_overlay_enabled": True,
            "auto_open_browser": False,
            "private_policy_stats_enabled": True,
            "low_frequency_listener_enabled": False,
        },
    )


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    timeout: float = 8.0,
) -> Any:
    request = urllib.request.Request(url, method=method, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _start_data_service(base_dir: Path, logs_dir: Path, *, timeout_seconds: float) -> DataServiceProcess:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stderr_stream = (logs_dir / "data-service.stderr.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hextech.data_service",
            "--parent-pid",
            str(os.getpid()),
            "--force-initial-refresh",
        ],
        cwd=RUN_DIR,
        env=_child_env(base_dir),
        stdout=subprocess.PIPE,
        stderr=stderr_stream,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    bootstrap_queue: queue.Queue[str] = queue.Queue(maxsize=1)
    stdout_log = logs_dir / "data-service.stdout.log"

    def _drain_stdout() -> None:
        if process.stdout is None:
            return
        first_line = process.stdout.readline()
        try:
            bootstrap_queue.put_nowait(first_line)
        except queue.Full:
            pass
        with stdout_log.open("w", encoding="utf-8") as stream:
            for line in process.stdout:
                stream.write(line)

    threading.Thread(target=_drain_stdout, daemon=True, name="acceptance-data-service-stdout").start()
    deadline = time.monotonic() + timeout_seconds
    line = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr_stream.close()
            raise AcceptanceFailure(f"DataService bootstrap 前退出：code={process.returncode}")
        try:
            line = bootstrap_queue.get(timeout=0.1)
            break
        except queue.Empty:
            continue
    if not line:
        _terminate_process_tree(process)
        stderr_stream.close()
        raise AcceptanceFailure("DataService bootstrap 超时")
    try:
        payload = json.loads(line)
        return DataServiceProcess(process, int(payload["port"]), str(payload["session_nonce"]), stderr_stream)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _terminate_process_tree(process)
        stderr_stream.close()
        raise AcceptanceFailure("DataService bootstrap 响应无效") from exc


def _data_service_headers(service: DataServiceProcess) -> dict[str, str]:
    return {"Host": "127.0.0.1", DATA_SERVICE_NONCE_HEADER: service.nonce}


def _wait_initial_refresh(service: DataServiceProcess, *, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    base = f"http://127.0.0.1:{service.port}"
    while time.monotonic() < deadline:
        if service.process.poll() is not None:
            raise AcceptanceFailure(f"DataService 刷新期间退出：code={service.process.returncode}")
        try:
            status = _http_json(base + "/v1/status", headers=_data_service_headers(service), timeout=3)
        except OSError:
            time.sleep(0.5)
            continue
        last_action = status.get("last_action") if isinstance(status, dict) else None
        if isinstance(last_action, dict) and last_action.get("type") == "refresh":
            result = last_action.get("result")
            if not isinstance(result, dict):
                raise AcceptanceFailure("DataService refresh 缺少结构化结果")
            if result.get("state") != "ready":
                raise AcceptanceFailure(
                    f"DataService 强制远端刷新失败：state={result.get('state')} reason={result.get('reason_code')}"
                )
            return dict(result)
        time.sleep(0.5)
    raise AcceptanceFailure(f"DataService 强制远端刷新超过 {timeout_seconds:.0f}s")


def _stop_data_service(service: DataServiceProcess) -> None:
    try:
        _http_json(
            f"http://127.0.0.1:{service.port}/v1/shutdown",
            method="POST",
            headers=_data_service_headers(service),
            timeout=2,
        )
    except OSError:
        pass
    try:
        service.process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(service.process)
    service.stderr_stream.close()


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _find_source_file(base_dir: Path, name: str, expected_sha256: str) -> Path:
    candidates = [
        path
        for path in (base_dir / "data").rglob(name)
        if path.is_file() and "snapshots" not in path.parts and _sha256(path) == expected_sha256
    ]
    if len(candidates) != 1:
        raise AcceptanceFailure(f"generation source 无法唯一映射：{name} count={len(candidates)}")
    return candidates[0]


def _parse_timestamp(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _verify_remote_and_generation(base_dir: Path, started_at: float) -> tuple[Any, dict[str, Any]]:
    from hextech.data_snapshot import DataSnapshotClient

    snapshot_root = base_dir / "data" / "runtime" / "snapshots"
    view = DataSnapshotClient(snapshot_root).open_view()
    status = view.status()
    if status.get("state") != "ready" or not status.get("generation_id"):
        raise AcceptanceFailure(f"generation 未 ready：{status}")
    manifest = view.manifest
    generation_dir = snapshot_root / "generations" / manifest.generation_id
    for item in manifest.files:
        path = generation_dir / item.relative_path
        if path.stat().st_size != item.size or _sha256(path) != item.sha256:
            raise AcceptanceFailure(f"generation 文件摘要不一致：{item.role}")
    if manifest.champion_count <= 0 or manifest.augment_count <= 0 or manifest.stat_record_count <= 0:
        raise AcceptanceFailure("generation 计数为空")
    if manifest.private_stats_enabled is not True:
        raise AcceptanceFailure("真实验收 generation 必须启用私用统计")

    sources: list[dict[str, Any]] = []
    for source in manifest.source_files:
        source_path = _find_source_file(base_dir, str(source.get("name") or ""), str(source.get("sha256") or ""))
        if source_path.stat().st_size != int(source.get("size") or -1):
            raise AcceptanceFailure(f"generation source size 不一致：{source_path.name}")
        if int(source.get("record_count") or 0) <= 0:
            raise AcceptanceFailure(f"generation source 记录为空：{source_path.name}")
        if source_path.stat().st_mtime < started_at - 1:
            raise AcceptanceFailure(f"generation source 不是本轮刷新：{source_path.name}")
        sources.append(
            {
                "name": source_path.name,
                "size": source_path.stat().st_size,
                "sha256": _sha256(source_path),
                "record_count": int(source.get("record_count") or 0),
            }
        )

    state_dir = base_dir / "data" / "runtime" / "state"
    scraper = _read_json(state_dir / "scraper_status.json")
    mayhem = _read_json(state_dir / "mayhem_refresh_status.json")
    if scraper.get("last_result") != "success" or bool(scraper.get("fallback_used")):
        raise AcceptanceFailure(
            f"Hextech 远端未真实成功：result={scraper.get('last_result')} fallback={scraper.get('fallback_used')}"
        )
    if mayhem.get("last_result") != "success" or int(mayhem.get("raw_items") or 0) <= 0:
        raise AcceptanceFailure(
            f"Mayhem 远端未真实成功：result={mayhem.get('last_result')} raw={mayhem.get('raw_items')}"
        )
    if _parse_timestamp(mayhem.get("last_attempt_at")) < started_at - 1:
        raise AcceptanceFailure("Mayhem 状态不是本轮刷新")
    mayhem_raw = base_dir / "data" / "runtime" / "cache" / "mayhem_combos.raw.json"
    if not mayhem_raw.is_file() or mayhem_raw.stat().st_mtime < started_at - 1:
        raise AcceptanceFailure("Mayhem raw 未在本轮发布")

    return view, {
        "remote_success": True,
        "fallback_used": False,
        "generation_id": manifest.generation_id,
        "created_at": manifest.created_at,
        "private_stats_enabled": manifest.private_stats_enabled,
        "champion_count": manifest.champion_count,
        "augment_count": manifest.augment_count,
        "stat_record_count": manifest.stat_record_count,
        "sources": sources,
        "scraper": {
            "last_result": scraper.get("last_result"),
            "success_rows": scraper.get("success_rows"),
            "failure_stage": scraper.get("failure_stage"),
        },
        "mayhem": {
            "last_result": mayhem.get("last_result"),
            "raw_items": mayhem.get("raw_items"),
            "added_items": mayhem.get("added_items"),
        },
    }


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(mapping.get(key) or "").strip()
        if text:
            return text
    return ""


def _metric(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value is None or value == "":
            continue
        text = str(value).strip()
        try:
            return float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
        except ValueError:
            continue
    return None


def _card_summary(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _first_text(card, "id", "海克斯ID", "augment_id", "augmentId"),
        "name": _first_text(card, "name", "海克斯名称", "augment_name", "augmentName"),
        "win_rate": _metric(card, "海克斯胜率", "win_rate", "winrate"),
        "pick_rate": _metric(card, "海克斯出场率", "pick_rate", "pickrate"),
    }


def select_acceptance_samples(view: Any) -> dict[str, Any]:
    """动态选择一个有统计组合和一个真实无统计组合。"""

    selected: dict[str, Any] | None = None
    for champion in view.get_champions():
        champion_id = _first_text(champion, "id", "英雄ID", "英雄 ID")
        champion_name = _first_text(champion, "name", "英雄名称")
        cards = view.get_champion_augments(champion_id or champion_name)
        for card in cards:
            summary = _card_summary(card)
            if summary["id"] and summary["name"] and summary["win_rate"] is not None and summary["pick_rate"] is not None:
                selected = {
                    "hero_id": champion_id,
                    "hero_name": champion_name,
                    "card": dict(card),
                    "card_summary": summary,
                    "hero_card_ids": {
                        _card_summary(item)["id"] for item in cards if _card_summary(item)["id"]
                    },
                }
                break
        if selected is not None:
            break
    if selected is None:
        raise AcceptanceFailure("generation 中找不到含胜率和出场率的真实样本")

    hints_payload = view.get_overlay_hints()
    hints = hints_payload.get("hints") if isinstance(hints_payload, Mapping) else None
    if not isinstance(hints, Mapping):
        raise AcceptanceFailure("generation overlay hints 缺失")
    no_stats: dict[str, Any] | None = None
    for augment_id, hint in hints.items():
        if not isinstance(hint, Mapping) or str(augment_id) in selected["hero_card_ids"]:
            continue
        augment_name = _first_text(hint, "name", "海克斯名称")
        if augment_name:
            no_stats = {"id": str(augment_id), "name": augment_name}
            break
    if no_stats is None:
        raise AcceptanceFailure("找不到真实英雄与真实海克斯的无统计组合")
    selected["no_stats_card"] = no_stats
    selected.pop("hero_card_ids", None)
    return selected


def _start_web(base_dir: Path, logs_dir: Path, *, timeout_seconds: float) -> tuple[subprocess.Popen[Any], str]:
    port_path = base_dir / "data" / "runtime" / "state" / "web_server_port.txt"
    port_path.unlink(missing_ok=True)
    stream = (logs_dir / "web.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, str(RUN_DIR / "hextech_ui.py"), "--web-server"],
        cwd=RUN_DIR,
        env=_child_env(base_dir),
        stdout=stream,
        stderr=subprocess.STDOUT,
    )
    process._acceptance_log_stream = stream  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stream.close()
            raise AcceptanceFailure(f"Web 提前退出：code={process.returncode}")
        try:
            port = port_path.read_text(encoding="utf-8").strip()
            if port:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as response:
                    if response.status == 200 and response.read():
                        return process, f"http://127.0.0.1:{port}"
        except OSError:
            pass
        time.sleep(0.25)
    _terminate_process_tree(process)
    stream.close()
    raise AcceptanceFailure("Web 启动超时")


def _stop_web(process: subprocess.Popen[Any]) -> None:
    _terminate_process_tree(process)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    stream = getattr(process, "_acceptance_log_stream", None)
    if stream is not None:
        stream.close()


def _find_matching_card(cards: object, expected_id: str) -> dict[str, Any] | None:
    if not isinstance(cards, list):
        return None
    for item in cards:
        if isinstance(item, dict) and _card_summary(item)["id"] == expected_id:
            return item
    return None


def _assert_metric_equal(actual: float | None, expected: float | None, label: str) -> None:
    if actual is None or expected is None or abs(actual - expected) > 1e-9:
        raise AcceptanceFailure(f"{label} 不一致：actual={actual} expected={expected}")


def _assert_display_metric(actual: object, expected: float | None, label: str) -> None:
    if expected is None:
        raise AcceptanceFailure(f"{label} 缺少原始统计")
    expected_text = f"{expected * 100.0:.1f}%" if abs(expected) <= 1.0 else f"{expected:.1f}%"
    if str(actual or "").strip() != expected_text:
        raise AcceptanceFailure(f"{label} 显示不一致：actual={actual} expected={expected_text}")


def _verify_web(base_url: str, view: Any, sample: Mapping[str, Any]) -> dict[str, Any]:
    champions = _http_json(base_url + "/api/champions")
    if not isinstance(champions, list) or not champions:
        raise AcceptanceFailure("Web 英雄列表为空")
    hero_name = str(sample["hero_name"])
    hero_id = str(sample["hero_id"])
    if not any(
        isinstance(item, Mapping)
        and hero_name == _first_text(item, "name", "英雄名称")
        and hero_id == _first_text(item, "id", "英雄ID", "英雄 ID")
        for item in champions
    ):
        raise AcceptanceFailure("Web 英雄列表缺少动态样本")
    detail = _http_json(base_url + "/api/champion/" + urllib.parse.quote(hero_name) + "/hextechs")
    if not isinstance(detail, dict) or detail.get("generation_id") != view.status().get("generation_id"):
        raise AcceptanceFailure("Web detail generation 与固定 view 不一致")
    expected = sample["card_summary"]
    card = _find_matching_card(detail.get("comprehensive"), str(expected["id"]))
    if card is None:
        raise AcceptanceFailure("Web detail 缺少动态统计卡")
    actual = _card_summary(card)
    if actual["name"] != expected["name"]:
        raise AcceptanceFailure("Web 海克斯名称不一致")
    _assert_metric_equal(actual["win_rate"], expected["win_rate"], "Web 胜率")
    _assert_metric_equal(actual["pick_rate"], expected["pick_rate"], "Web 出场率")
    synergy = _http_json(base_url + "/api/synergies/" + urllib.parse.quote(hero_id))
    if not isinstance(synergy, dict) or not isinstance(synergy.get("synergies"), list):
        raise AcceptanceFailure("Web synergy API 无有效 payload")
    return {
        "generation_id": detail.get("generation_id"),
        "hero_id": hero_id,
        "hero_name": hero_name,
        "card": actual,
        "synergy_count": len(synergy["synergies"]),
    }


def _event_heartbeat(path: Path, payload: Mapping[str, Any], stop: threading.Event) -> None:
    while not stop.is_set():
        current = dict(payload)
        current["generated_at"] = time.time()
        timing = current.get("timing") if isinstance(current.get("timing"), Mapping) else {}
        current["timing"] = {**dict(timing), "event_written_at": time.time()}
        _write_json(path, current)
        stop.wait(0.5)


def _run_tk_screenshot(
    command: list[str],
    *,
    env: Mapping[str, str],
    event_path: Path,
    event_payload: Mapping[str, Any],
    output_path: Path,
    log_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_event_heartbeat,
        args=(event_path, event_payload, stop),
        daemon=True,
        name="acceptance-overlay-event-heartbeat",
    )
    heartbeat.start()
    try:
        completed = subprocess.run(
            command,
            cwd=Path(command[0]).parent if Path(command[0]).suffix.lower() == ".exe" else RUN_DIR,
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    finally:
        stop.set()
        heartbeat.join(timeout=2)
    log_path.write_text(completed.stdout + "\n--- STDERR ---\n" + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise AcceptanceFailure(f"Tk screenshot 失败：code={completed.returncode}")
    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceFailure("Tk screenshot 未输出结构化结果") from exc
    if not isinstance(summary, dict) or not summary.get("ok"):
        raise AcceptanceFailure("Tk screenshot 结果未通过")
    _verify_nonblank_image(output_path)
    return summary


def _verify_nonblank_image(path: Path) -> None:
    from PIL import Image

    if not path.is_file() or path.stat().st_size <= 0:
        raise AcceptanceFailure(f"Overlay 截图为空：{path}")
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        colors = rgb.getcolors(maxcolors=1_000_000)
        if rgb.width < 640 or rgb.height < 360 or colors is None or len(colors) < 2:
            raise AcceptanceFailure(f"Overlay 截图像素无有效内容：{path}")


def _overlay_event_and_context(view: Any, sample: Mapping[str, Any], card: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from hextech.overlay.context import build_overlay_context_payload, write_overlay_context
    from hextech.overlay.data_source import SharedOverlayDataSource
    from hextech.overlay.events import build_overlay_event, write_overlay_event
    from hextech.overlay.renderer import build_render_model

    hint_cache = SharedOverlayDataSource().read_hint_cache()
    context = build_overlay_context_payload(
        champion_id=sample["hero_id"],
        champion_name=sample["hero_name"],
        source="acceptance",
        phase="champ_select",
        connection_state="connected",
    )
    write_overlay_context(context)
    event = build_overlay_event(
        [{"slot": 0, "augment_id": card["id"], "name": card["name"], "state": "ready"}],
        source_tag="acceptance",
        hint_cache=hint_cache,
    )
    write_overlay_event(event)
    source = SharedOverlayDataSource()
    model = build_render_model(source.read_event(), hint_cache=source.read_hint_cache(), context=source.read_context())
    if source.read_hint_cache().get("snapshot", {}).get("generation_id") != view.status().get("generation_id"):
        raise AcceptanceFailure("Overlay generation 与 Web/固定 view 不一致")
    return event, context, model


def _status_row(model: Mapping[str, Any], code: str) -> Mapping[str, Any] | None:
    rows = model.get("stats")
    if not isinstance(rows, list):
        return None
    return next((row for row in rows if isinstance(row, Mapping) and row.get("status_code") == code), None)


def _verify_source_overlay(
    artifact_dir: Path,
    base_dir: Path,
    view: Any,
    sample: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    expected = sample["card_summary"]
    ready_event, context, ready_model = _overlay_event_and_context(view, sample, expected)
    ready_row = _status_row(ready_model, "READY")
    if ready_row is None:
        raise AcceptanceFailure("Overlay ready 样本没有 READY 行")
    _assert_display_metric(ready_row.get("winrate_text"), expected["win_rate"], "Overlay 胜率")
    _assert_display_metric(ready_row.get("pickrate_text"), expected["pick_rate"], "Overlay 出场率")

    state_dir = base_dir / "data" / "runtime" / "state"
    ready_png = artifact_dir / "overlay-ready.png"
    source_command = [
        sys.executable,
        str(RUN_DIR / "hextech_ui.py"),
        "--game-overlay",
        "--acceptance-screenshot",
        str(ready_png),
    ]
    ready_summary = _run_tk_screenshot(
        source_command,
        env=_child_env(base_dir),
        event_path=state_dir / "game_overlay_slots.v1.json",
        event_payload=ready_event,
        output_path=ready_png,
        log_path=artifact_dir / "logs" / "overlay-ready.log",
        timeout_seconds=60,
    )
    if int(ready_summary.get("status_counts", {}).get("READY") or 0) <= 0:
        raise AcceptanceFailure("真实 Tk 截图没有 READY 统计")

    no_stats = sample["no_stats_card"]
    no_event, _, no_model = _overlay_event_and_context(view, sample, no_stats)
    if _status_row(no_model, "NO_STATS") is None:
        raise AcceptanceFailure("真实无统计组合未显示 NO_STATS")
    no_png = artifact_dir / "overlay-no-stats.png"
    no_summary = _run_tk_screenshot(
        source_command[:-1] + [str(no_png)],
        env=_child_env(base_dir),
        event_path=state_dir / "game_overlay_slots.v1.json",
        event_payload=no_event,
        output_path=no_png,
        log_path=artifact_dir / "logs" / "overlay-no-stats.log",
        timeout_seconds=60,
    )
    if int(no_summary.get("status_counts", {}).get("NO_STATS") or 0) <= 0:
        raise AcceptanceFailure("真实 Tk 无统计截图没有 NO_STATS")
    return {
        "generation_id": ready_summary.get("generation_id"),
        "ready_status_counts": ready_summary.get("status_counts"),
        "no_stats_status_counts": no_summary.get("status_counts"),
        "ready_screenshot": str(ready_png),
        "no_stats_screenshot": str(no_png),
    }, ready_event, context


def _verify_failed_generation_fallback(base_url: str, snapshot_root: Path, view: Any, sample: Mapping[str, Any]) -> dict[str, Any]:
    from hextech.data_service import DataServiceCore
    from hextech.data_snapshot import DataSnapshotClient, DataSnapshotPublisher
    from hextech.display.desktop.app import HextechUI
    from hextech.overlay.data_source import SharedOverlayDataSource
    import threading as threading_module
    from types import MethodType, SimpleNamespace

    generation_id = str(view.status().get("generation_id") or "")

    def _fail_builder(_enabled: bool):
        raise RuntimeError("acceptance_next_generation_failure")

    result = DataServiceCore(
        publisher=DataSnapshotPublisher(snapshot_root),
        builder=_fail_builder,
        private_stats_enabled=True,
    ).refresh()
    if result.get("state") != "degraded" or result.get("generation_id") != generation_id:
        raise AcceptanceFailure(f"失败代没有保留 last-good：{result}")
    if result.get("reason_code") != "refresh_failed_last_good_preserved":
        raise AcceptanceFailure("失败代缺少明确降级原因")

    detail = _http_json(base_url + "/api/champion/" + urllib.parse.quote(str(sample["hero_name"])) + "/hextechs")
    if not isinstance(detail, dict) or detail.get("generation_id") != generation_id:
        raise AcceptanceFailure("失败代后 Web 未保留原 generation")

    rendered: list[dict[str, Any]] = []
    ui = SimpleNamespace(
        _snapshot_client=DataSnapshotClient(snapshot_root),
        _snapshot_generation_id="",
        _champions_lock=threading_module.Lock(),
        champions=[],
        current_candidate_groups={"selected_champion_ids": [sample["hero_id"]], "bench_champion_ids": []},
        _set_status=lambda *_args: None,
        update_ui=lambda groups: rendered.append(dict(groups)),
        _run_on_ui_thread=lambda callback: callback() or True,
    )
    ui.load_data = MethodType(HextechUI.load_data, ui)
    loaded_champions = ui.load_data()
    if not loaded_champions:
        raise AcceptanceFailure("失败代后桌面 loader 未保留英雄数据")
    ui.champions = loaded_champions
    overlay_generation = SharedOverlayDataSource(
        snapshot_client=DataSnapshotClient(snapshot_root)
    ).read_hint_cache().get("snapshot", {}).get("generation_id")
    if overlay_generation != generation_id:
        raise AcceptanceFailure("失败代后 Overlay 未保留原 generation")
    return {
        "generation_id": generation_id,
        "reason_code": result.get("reason_code"),
        "web_generation_id": detail.get("generation_id"),
        "desktop_rows": len(ui.champions),
        "overlay_generation_id": overlay_generation,
    }


def _latest_package(releases_dir: Path) -> Path:
    packages = [path for path in releases_dir.glob("HextechCompanion-*") if path.is_dir()]
    if not packages:
        raise AcceptanceFailure(f"构建后未找到便携包：{releases_dir}")
    return max(packages, key=lambda path: path.stat().st_mtime)


def _build_verified_package(
    snapshot_root: Path,
    artifact_dir: Path,
    *,
    timeout_seconds: float,
) -> Path:
    log_path = artifact_dir / "logs" / "build-package.log"
    build_env = os.environ.copy()
    build_env.pop("HEXTECH_BASE_DIR", None)
    with log_path.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(
            [
                sys.executable,
                str(RUN_DIR / "tools" / "build_package.py"),
                "--verified-snapshot-root",
                str(snapshot_root),
            ],
            cwd=RUN_DIR,
            env=build_env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    if completed.returncode != 0:
        raise AcceptanceFailure(f"PyInstaller 构建失败，见 {log_path}")
    return _latest_package(WORKTREE_DIR / ".artifacts" / "hextech" / "releases")


def _processes_under(path: Path) -> list[int]:
    import psutil

    root = str(path.resolve()).casefold()
    residual: list[int] = []
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            executable = str(process.info.get("exe") or "")
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if executable and executable.casefold().startswith(root):
            residual.append(int(process.info["pid"]))
    return residual


def _verify_packaged(
    package_dir: Path,
    artifact_dir: Path,
    view: Any,
    sample: Mapping[str, Any],
    ready_event: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    from tools.acceptance.smoke_packaged_startup import _copy_clean_package, _find_exe, run_smoke

    smoke_root = artifact_dir / "package-smoke"
    copied_package = _copy_clean_package(package_dir, smoke_root)
    smoke = run_smoke(copied_package, timeout_seconds)
    if not smoke.get("ok") or not smoke.get("verified_snapshot_seeded"):
        raise AcceptanceFailure(f"BAT packaged smoke 未通过：{smoke.get('last_error')}")
    expected_generation = str(view.status().get("generation_id") or "")
    detail = smoke.get("web", {}).get("representative_detail", {})
    if detail.get("generation_id") != expected_generation or int(detail.get("card_count") or 0) <= 0:
        raise AcceptanceFailure("packaged Web 未读取已验证 generation")
    representative = smoke.get("web", {}).get("representative", {})
    source_detail = view.get_champion_detail(representative.get("hero_id") or representative.get("hero"))
    source_cards = source_detail.get("augments") if isinstance(source_detail, Mapping) else []
    packaged_card = detail.get("first_card") if isinstance(detail, Mapping) else None
    packaged_summary = _card_summary(packaged_card) if isinstance(packaged_card, Mapping) else {}
    source_card = _find_matching_card(source_cards, str(packaged_summary.get("id") or ""))
    if source_card is None:
        raise AcceptanceFailure("packaged Web 样本无法映射回已验证 generation")
    source_summary = _card_summary(source_card)
    _assert_metric_equal(packaged_summary.get("win_rate"), source_summary.get("win_rate"), "packaged Web 胜率")
    _assert_metric_equal(packaged_summary.get("pick_rate"), source_summary.get("pick_rate"), "packaged Web 出场率")

    runtime_root = Path(str(smoke["runtime_root"]))
    state_dir = runtime_root / "state"
    event_path = state_dir / "game_overlay_slots.v1.json"
    context_path = state_dir / "game_overlay_context.v1.json"
    _write_json(event_path, ready_event)
    _write_json(context_path, context)
    exe = _find_exe(copied_package)
    screenshot = artifact_dir / "packaged-overlay-ready.png"
    local_app_data = runtime_root.parents[2]
    packaged_env = os.environ.copy()
    packaged_env["LOCALAPPDATA"] = str(local_app_data)
    packaged_env["APPDATA"] = str(local_app_data.parent / "Roaming")
    packaged_summary_result = _run_tk_screenshot(
        [
            str(exe),
            "--game-overlay",
            "--acceptance-screenshot",
            str(screenshot),
        ],
        env=packaged_env,
        event_path=event_path,
        event_payload=ready_event,
        output_path=screenshot,
        log_path=artifact_dir / "logs" / "packaged-overlay.log",
        timeout_seconds=90,
    )
    if packaged_summary_result.get("generation_id") != expected_generation:
        raise AcceptanceFailure("packaged Overlay 与 Web generation 不一致")
    time.sleep(1)
    residual = _processes_under(copied_package)
    if residual:
        raise AcceptanceFailure(f"packaged 验收退出后仍有残留进程：{residual}")
    return {
        "package_dir": str(package_dir),
        "smoke_package_dir": str(copied_package),
        "bat_elapsed_seconds": smoke.get("elapsed_seconds"),
        "generation_id": expected_generation,
        "web_card": packaged_summary,
        "overlay_status_counts": packaged_summary_result.get("status_counts"),
        "overlay_screenshot": str(screenshot),
        "residual_pids": residual,
    }


def _report_path(artifact_dir: Path) -> Path:
    return artifact_dir / "report.json"


def verify_real_session_evidence(path: Path, *, expected_generation_id: str) -> dict[str, Any]:
    """验证真实 LCU、窗口、Vision、推荐与截图属于同一局。

    合成 event/Tk 结果不能调用本函数冒充真实证据；采集器必须显式写
    ``evidence_kind=real_game_session``。
    """

    payload = _read_json(path)
    if payload.get("evidence_kind") != "real_game_session":
        raise AcceptanceFailure("缺少真实游戏会话证据标记")
    generation_id = str(payload.get("generation_id") or "")
    session_id = str(payload.get("session_id") or "")
    if not generation_id or generation_id != expected_generation_id:
        raise AcceptanceFailure("真实会话 generation 与已验证快照不一致")
    if not session_id:
        raise AcceptanceFailure("真实会话缺少 session_id")
    required_sections = ("lcu", "window", "vision", "recommendation", "final_state")
    for section_name in required_sections:
        section = payload.get(section_name)
        if not isinstance(section, Mapping):
            raise AcceptanceFailure(f"真实会话缺少 {section_name} 证据")
        section_session = str(section.get("session_id") or "")
        if section_session != session_id:
            raise AcceptanceFailure(f"真实会话 session 不一致：{section_name}")
    lcu = payload["lcu"]
    window = payload["window"]
    vision = payload["vision"]
    recommendation = payload["recommendation"]
    final_state = payload["final_state"]
    if not str(lcu.get("local_champion_id") or ""):
        raise AcceptanceFailure("真实 LCU 未取得本地英雄")
    if (
        int(window.get("hwnd") or 0) <= 0
        or not window.get("client_size")
        or not window.get("capture_size")
        or float(window.get("dpi_scale") or 0.0) <= 0
    ):
        raise AcceptanceFailure("真实游戏窗口证据不完整")
    if int(vision.get("epoch") or 0) <= 0 or len(vision.get("slots") or []) != 3:
        raise AcceptanceFailure("真实 Vision epoch 或三槽证据不完整")
    if str(recommendation.get("generation_id") or "") != generation_id:
        raise AcceptanceFailure("真实推荐 generation 不一致")
    if str(final_state.get("generation_id") or "") != generation_id:
        raise AcceptanceFailure("真实渲染 generation 不一致")
    if int(final_state.get("vision_epoch") or 0) != int(vision.get("epoch") or 0):
        raise AcceptanceFailure("真实渲染 Vision epoch 不一致")
    if not bool(final_state.get("should_show")) or str(final_state.get("presentation_mode") or "") != "content":
        raise AcceptanceFailure("真实 Overlay 最终未进入可见内容态")
    screenshot = Path(str(payload.get("screenshot") or ""))
    if not screenshot.is_absolute():
        screenshot = path.parent / screenshot
    if not screenshot.is_file() or screenshot.stat().st_size <= 0:
        raise AcceptanceFailure("真实 Overlay 非空截图缺失")
    return {
        "generation_id": generation_id,
        "session_id": session_id,
        "vision_epoch": int(vision["epoch"]),
        "screenshot": str(screenshot),
    }


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = (args.artifact_root / timestamp).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=False)
    (artifact_dir / "logs").mkdir()
    base_dir = artifact_dir / "isolated-root"
    report: dict[str, Any] = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifact_dir": str(artifact_dir),
        "stages": {},
    }
    _write_json(_report_path(artifact_dir), report)
    service: DataServiceProcess | None = None
    web_process: subprocess.Popen[Any] | None = None
    try:
        _prepare_isolated_root(base_dir)
        started_at = time.time()
        service = _start_data_service(base_dir, artifact_dir / "logs", timeout_seconds=30)
        refresh_result = _wait_initial_refresh(service, timeout_seconds=args.remote_timeout)
        os.environ["HEXTECH_BASE_DIR"] = str(base_dir)
        view, generation_report = _verify_remote_and_generation(base_dir, started_at)
        report["stages"]["remote_generation"] = {
            **generation_report,
            "data_service_result": refresh_result,
        }
        _write_json(_report_path(artifact_dir), report)

        sample = select_acceptance_samples(view)
        report["sample"] = {
            "hero_id": sample["hero_id"],
            "hero_name": sample["hero_name"],
            "with_stats": sample["card_summary"],
            "without_stats": sample["no_stats_card"],
        }
        web_process, base_url = _start_web(base_dir, artifact_dir / "logs", timeout_seconds=args.web_timeout)
        report["stages"]["web"] = _verify_web(base_url, view, sample)
        overlay_report, ready_event, context = _verify_source_overlay(artifact_dir, base_dir, view, sample)
        report["stages"]["overlay"] = overlay_report
        _stop_data_service(service)
        service = None
        report["stages"]["failed_generation"] = _verify_failed_generation_fallback(
            base_url,
            base_dir / "data" / "runtime" / "snapshots",
            view,
            sample,
        )
        _stop_web(web_process)
        web_process = None
        _write_json(_report_path(artifact_dir), report)

        package_dir = _build_verified_package(
            base_dir / "data" / "runtime" / "snapshots",
            artifact_dir,
            timeout_seconds=args.build_timeout,
        )
        report["stages"]["packaged"] = _verify_packaged(
            package_dir,
            artifact_dir,
            view,
            sample,
            ready_event,
            context,
            timeout_seconds=args.package_timeout,
        )
        report["component_chain_ok"] = True
        if args.real_session_evidence is None:
            if not args.component_only:
                raise AcceptanceFailure("缺少 --real-session-evidence；合成 Overlay 只能算组件测试")
        else:
            report["stages"]["real_session"] = verify_real_session_evidence(
                args.real_session_evidence.resolve(),
                expected_generation_id=str(view.status().get("generation_id") or ""),
            )
            report["ok"] = True
        report["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_json(_report_path(artifact_dir), report)
        return report
    except Exception as exc:
        report["failed_stage"] = next(
            (
                name
                for name in ("remote_generation", "web", "overlay", "failed_generation", "packaged")
                if name not in report["stages"]
            ),
            "finalize",
        )
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        report["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_json(_report_path(artifact_dir), report)
        raise
    finally:
        if service is not None:
            _stop_data_service(service)
        if web_process is not None:
            _stop_web(web_process)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="真实验收 remote -> generation -> Web -> Overlay -> packaged BAT/EXE 数据链。")
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--remote-timeout", type=int, default=900)
    parser.add_argument("--web-timeout", type=int, default=60)
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument("--package-timeout", type=int, default=70)
    parser.add_argument("--real-session-evidence", type=Path, default=None)
    parser.add_argument(
        "--component-only",
        action="store_true",
        help="只运行抓取、Web、合成 Tk 和打包组件链；报告 ok 仍保持 false。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_acceptance(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
