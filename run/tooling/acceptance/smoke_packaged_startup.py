"""打包产物空仓首启烟测。

这个文件用于验证 PyInstaller 便携包在隔离目录首次启动时，是否能真实拉起
App → Overlay Host + Vision Sidecar 三进程全链。
它只负责本地验收，不负责构建产物、不替代真实 League 窗口验收、不修改业务数据。

调用方: dev_checks; 关键依赖: 见 imports。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil


RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
# package.py 以脚本路径启动 smoke；当前 worktree 的 run/src 必须排在全局
# editable install 之前，否则会误导入母仓旧版 hextech。
for import_root in (SRC_DIR, RUN_DIR):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from tooling.build.manifest import BUNDLE_MANIFEST_SCHEMA_VERSION, RUNTIME_CONTRACT_VERSIONS  # noqa: E402


REQUIRED_PACKAGE_DIRS = (
    "resources/catalog",
    "resources/assets",
    "resources/seeds/runtime.v4/snapshots/generations",
)

REQUIRED_RUNTIME_DIRS = (
    "state",
    "locks",
    "profiles",
    "snapshots",
)

REQUIRED_RUNTIME_FILES = (
    "state/startup_status.json",
)
OVERLAY_ANCHOR_CALIBRATION_FILENAME = "overlay_anchor_calibration.v1.json"
FORBIDDEN_PACKAGE_PATHS = (
    "var",
    "data",
    "tests",
    "tooling",
)
FORBIDDEN_PACKAGE_GENERATED_SUFFIXES = (".pyc", ".pyo")
SMOKE_FEATURE_FLAGS = {
    "game_overlay_enabled": True,
    "private_policy_stats_enabled": True,
    "low_frequency_listener_enabled": True,
}


class SmokeFailure(RuntimeError):
    pass


def _validate_windows_gui_subsystem(exe_path: Path) -> None:
    """确认 PE 使用 Windows GUI subsystem，防止桌面快捷方式再次弹终端。"""

    if os.name != "nt":
        return
    try:
        with exe_path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise SmokeFailure("主程序不是有效 PE 文件")
            stream.seek(0x3C)
            pe_offset = struct.unpack("<I", stream.read(4))[0]
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                raise SmokeFailure("主程序缺少 PE signature")
            stream.seek(pe_offset + 4 + 20 + 68)
            subsystem = struct.unpack("<H", stream.read(2))[0]
    except (OSError, struct.error) as exc:
        raise SmokeFailure(f"无法读取主程序 PE subsystem：{exc}") from exc
    if subsystem != 2:  # IMAGE_SUBSYSTEM_WINDOWS_GUI
        raise SmokeFailure(f"主程序必须使用 Windows GUI subsystem，实际={subsystem}")


def _validate_bundle_contract(package_dir: Path) -> dict[str, object]:
    """启动前验证 v3 构建身份，旧包不得通过新 smoke。"""

    manifest_path = _packaged_data_root(package_dir) / "bundle_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"bundle manifest 无法读取：{exc}") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != BUNDLE_MANIFEST_SCHEMA_VERSION:
        raise SmokeFailure("bundle manifest 必须为 v3")
    if not str(payload.get("build_id") or "").strip():
        raise SmokeFailure("bundle manifest 缺少 build_id")
    if payload.get("runtime_contracts") != RUNTIME_CONTRACT_VERSIONS:
        raise SmokeFailure("bundle manifest 运行契约不匹配")
    if payload.get("runtime_profile") not in {"installed", "isolated_candidate"}:
        raise SmokeFailure("bundle manifest 必须声明有效 runtime_profile")
    cohort = payload.get("cohort_seed")
    if cohort:
        if (
            not isinstance(cohort, dict)
            or int(cohort.get("schema_version") or 0) != 3
            or int(cohort.get("production_pool_count") or 0) <= 0
            or not payload.get("cohort_seed_files")
            or not payload.get("cohort_seed_sha256")
        ):
            raise SmokeFailure("bundle manifest cohort seed 合同无效")
        if int(cohort["schema_version"]) == 3 and (
            cohort.get("snapshot_schema_version") != 4 or not isinstance(cohort.get("units"), dict)
            or not cohort["units"]
        ):
            raise SmokeFailure("v4 cohort seed 必须声明完整 units 闭包")
        if "recognition_catalog_generation_id" in cohort and not str(
            cohort.get("recognition_catalog_generation_id") or ""
        ).strip():
            raise SmokeFailure("cohort seed recognition Catalog identity 无效")
    return payload


def _latest_package(dist_dir: Path) -> Path:
    if not dist_dir.is_dir():
        raise SmokeFailure(f"未找到打包搜索目录：{dist_dir}")
    packages = [
        p for p in dist_dir.iterdir()
        if p.is_dir() and (p.name.startswith("HextechCompanion-") or p.name.startswith("Hextech_"))
    ]
    if not packages:
        raise SmokeFailure(f"未找到打包目录：{dist_dir}")
    return max(packages, key=lambda p: p.stat().st_mtime)


def _copy_clean_package(source: Path, smoke_root: Path) -> Path:
    if smoke_root.exists() and any(smoke_root.iterdir()):
        raise SmokeFailure("smoke root 已有证据；请使用新的隔离目录")
    smoke_root.mkdir(parents=True, exist_ok=True)
    target = smoke_root / source.name
    shutil.copytree(source, target)
    return target


def _cleanup_smoke_root(smoke_root: Path, *, attempts: int = 20, delay_seconds: float = 0.25) -> bool:
    """等待 Windows 子进程释放 runtime 句柄后清理隔离烟测目录。"""

    for attempt in range(max(1, attempts)):
        shutil.rmtree(smoke_root, ignore_errors=True)
        if not smoke_root.exists():
            return True
        if attempt + 1 < attempts:
            time.sleep(max(0.0, delay_seconds))
    return False


def _find_exe(package_dir: Path) -> Path:
    exes = list(package_dir.glob("*.exe"))
    if len(exes) != 1:
        raise SmokeFailure(f"打包目录必须且只能包含一个根 exe：count={len(exes)} path={package_dir}")
    return exes[0]


def _find_launcher(package_dir: Path) -> Path:
    launchers = list(package_dir.glob("*.bat"))
    if len(launchers) != 1:
        raise SmokeFailure(f"打包目录必须且只能包含一个根 BAT：count={len(launchers)} path={package_dir}")
    return launchers[0]


def _get_packaged_runtime_root(env: dict[str, str] | None = None) -> Path:
    source_env = os.environ if env is None else env
    local_app_data = source_env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "HextechNexus" / "var"
    app_data = source_env.get("APPDATA", "").strip()
    if app_data:
        return Path(app_data) / "HextechNexus" / "var"
    return Path.home() / ".hextech_nexus" / "var"


def _smoke_runtime_environment(
    package_dir: Path, fixture: str, manifest: dict[str, object],
) -> tuple[dict[str, str], Path]:
    """候选靠 frozen profile 隔离；installed 才使用 AppData sandbox。"""
    env = os.environ.copy()
    env.pop("HEXTECH_VAR_DIR", None)
    if manifest.get("runtime_profile") == "isolated_candidate":
        return env, package_dir.resolve() / "var"
    if manifest.get("runtime_profile") != "installed":
        raise SmokeFailure("bundle manifest 必须声明有效 runtime_profile")
    appdata_root = package_dir.parent / f"appdata-{fixture}"
    env["LOCALAPPDATA"] = str(appdata_root / "Local")
    env["APPDATA"] = str(appdata_root / "Roaming")
    return env, _get_packaged_runtime_root(env)


def _runtime_metadata_snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    """只记录正式 var 的元数据；不打开文件内容，不跟随链接或 junction。"""
    snapshot: dict[str, tuple[int, int, int]] = {}
    try:
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            return {}
        pending = [(root, root_metadata)]
        while pending:
            path, metadata = pending.pop()
            snapshot[path.relative_to(root).as_posix()] = (metadata.st_mode, metadata.st_size, metadata.st_mtime_ns)
            is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & 0x400)
            if is_reparse or stat.S_ISLNK(metadata.st_mode):
                raise SmokeFailure("正式 runtime 包含链接；无法在限定元数据范围证明未变更")
            if stat.S_ISDIR(metadata.st_mode):
                # scandir 的 Windows 元数据来自目录枚举，避免每个文件反复 stat。
                with os.scandir(path) as entries:
                    pending.extend((Path(entry.path), entry.stat(follow_symlinks=False)) for entry in entries)
    except OSError as exc:
        raise SmokeFailure(f"正式 runtime 元数据快照失败：{type(exc).__name__}") from exc
    return snapshot


def _packaged_data_root(package_dir: Path) -> Path:
    return package_dir / "_internal" if (package_dir / "_internal").exists() else package_dir


def _has_verified_snapshot_seed(package_dir: Path) -> bool:
    return (_packaged_data_root(package_dir) / "resources" / "seeds" / "runtime.v4" / "snapshots" / "current.v4.json").is_file()


def _has_verified_cohort_seed(package_dir: Path) -> bool:
    return (_packaged_data_root(package_dir) / "resources" / "seeds" / "runtime.v4" / "snapshots" / "current.v4.json").is_file()


def _write_smoke_feature_flags(runtime_root: Path) -> None:
    """烟测显式打开 Overlay/私用统计。"""

    state_dir = runtime_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "ui_feature_flags.json").write_text(
        json.dumps(SMOKE_FEATURE_FLAGS, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_json_file(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _expected_generation_id(bundle_manifest: dict[str, object]) -> str:
    for key in ("cohort_seed", "seed_health"):
        value = bundle_manifest.get(key)
        if isinstance(value, dict) and str(value.get("generation_id") or "").strip():
            return str(value["generation_id"])
    return ""


def _process_alive(pid_value: object) -> bool:
    try:
        pid = int(pid_value or 0)
    except (TypeError, ValueError):
        return False
    return pid > 0 and psutil.pid_exists(pid)


def _overlay_chain_status(
    runtime_root: Path,
    bundle_manifest: dict[str, object],
    *,
    desktop_pid: int,
    started_at_wall: float,
) -> tuple[dict[str, bool], dict[str, object]]:
    """读取当前 Build 的全链状态；旧 Sidecar 文件不能单独满足任何 ready 门。"""

    expected_build = str(bundle_manifest.get("build_id") or "")
    expected_generation = _expected_generation_id(bundle_manifest)
    startup_path = runtime_root / "state" / "startup_status.json"
    host_path = runtime_root / "state" / "game_overlay_visibility.v1.json"
    sidecar_path = runtime_root / "state" / "game_overlay_sidecar_status.json"
    report_path = runtime_root / "reports" / "overlay_sessions" / "latest.json"
    startup = _read_json_file(startup_path)
    host = _read_json_file(host_path)
    sidecar = _read_json_file(sidecar_path)
    report = _read_json_file(report_path)
    snapshot = startup.get("data_snapshot") if isinstance(startup.get("data_snapshot"), dict) else {}
    raw_stats_scope = report.get("stats_scope")
    stats_scope: dict[str, object] = dict(raw_stats_scope) if isinstance(raw_stats_scope, dict) else {}
    sidecar_matrix_rows = sidecar.get("matrix_rows") if isinstance(sidecar.get("matrix_rows"), dict) else {}
    host_updated = float(host.get("updated_at") or 0.0)
    sidecar_heartbeat = float(sidecar.get("heartbeat_at") or 0.0)
    report_recorded = float(report.get("recorded_at") or 0.0)
    checks = {
        "desktop_pid_alive": _process_alive(desktop_pid),
        "app_snapshot_ready": snapshot.get("state") in {"ready", "degraded"},
        "app_generation": bool(expected_generation) and str(snapshot.get("generation_id") or "") == expected_generation,
        "three_distinct_processes": len({desktop_pid, int(host.get("pid") or 0), int(sidecar.get("pid") or 0)}) == 3,
        "host_schema": int(host.get("schema_version") or 0) == 2,
        "host_build": str(host.get("build_id") or "") == expected_build,
        "host_pid_alive": _process_alive(host.get("pid")),
        "host_fresh": host_updated >= started_at_wall,
        "host_stats_generation": bool(expected_generation)
        and str(host.get("stats_generation_id") or host.get("data_generation_id") or "")
        == expected_generation,
        "host_generation_roles": str(host.get("vision_pool_generation_id") or "") == expected_generation
        and isinstance(host.get("generation_roles"), dict),
        "sidecar_schema": int(sidecar.get("schema_version") or 0) == 2,
        "sidecar_build": str(sidecar.get("build_id") or "") == expected_build,
        "sidecar_running": sidecar.get("status") == "running",
        "sidecar_pid_alive": _process_alive(sidecar.get("pid")),
        "sidecar_fresh": sidecar_heartbeat >= started_at_wall,
        "sidecar_vision_pool_generation": bool(expected_generation)
        and str(sidecar.get("vision_pool_generation_id") or sidecar.get("data_generation_id") or "")
        == expected_generation,
        "sidecar_generation_roles": str(sidecar.get("stats_generation_id") or "") == ""
        and isinstance(sidecar.get("generation_roles"), dict),
        "sidecar_observed_name_bound": int(sidecar_matrix_rows.get("observed_name") or 0) > 0,
        "session_report_schema": int(report.get("schema_version") or 0) == 2,
        "session_report_build": str(report.get("build_id") or "") == expected_build,
        "session_report_fresh": report_recorded >= started_at_wall,
        "session_report_stats_scope": bool(stats_scope)
        and isinstance(stats_scope.get("stage_context"), dict)
        and "status" in stats_scope,
        "session_report_generation_roles": str(report.get("stats_generation_id") or "")
        == expected_generation
        and str(report.get("vision_pool_generation_id") or "") == expected_generation
        and isinstance(report.get("generation_roles"), dict),
    }
    if bundle_manifest.get("runtime_profile") == "isolated_candidate":
        timing = _read_json_file(runtime_root / "state" / "startup_timing.v1.json")
        for label, payload in (("app", timing), ("host", host), ("sidecar", sidecar)):
            actual_root = str(payload.get("runtime_root") or "").strip()
            checks[f"{label}_runtime_root"] = bool(actual_root) and Path(actual_root).resolve() == runtime_root.resolve()
    details: dict[str, object] = {
        "expected_build_id": expected_build,
        "expected_generation_id": expected_generation,
        "snapshot_generation_id": str(snapshot.get("generation_id") or ""),
        "host_pid": int(host.get("pid") or 0),
        "host_updated_at": host_updated,
        "sidecar_pid": int(sidecar.get("pid") or 0),
        "sidecar_heartbeat_at": sidecar_heartbeat,
        "vision_pool_generation_id": str(
            sidecar.get("vision_pool_generation_id") or sidecar.get("data_generation_id") or ""
        ),
        "stats_generation_id": str(
            host.get("stats_generation_id") or host.get("data_generation_id") or ""
        ),
        "report_recorded_at": report_recorded,
        "report_generation_id": str(report.get("generation_id") or ""),
        "stats_scope": stats_scope,
    }
    return checks, details


def _wait_for_overlay_heartbeats(
    runtime_root: Path,
    *,
    host_pid: int,
    sidecar_pid: int,
    host_updated_at: float,
    sidecar_heartbeat_at: float,
    timeout_seconds: float = 8.0,
) -> dict[str, object]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        host = _read_json_file(runtime_root / "state" / "game_overlay_visibility.v1.json")
        sidecar = _read_json_file(runtime_root / "state" / "game_overlay_sidecar_status.json")
        next_host = float(host.get("updated_at") or 0.0)
        next_sidecar = float(sidecar.get("heartbeat_at") or 0.0)
        if (
            int(host.get("pid") or 0) == host_pid
            and int(sidecar.get("pid") or 0) == sidecar_pid
            and _process_alive(host_pid)
            and _process_alive(sidecar_pid)
            and next_host > host_updated_at
            and next_sidecar > sidecar_heartbeat_at
        ):
            return {
                "host_advanced": True,
                "sidecar_advanced": True,
                "host_updated_at": next_host,
                "sidecar_heartbeat_at": next_sidecar,
            }
        time.sleep(0.25)
    raise SmokeFailure("Host/Sidecar 心跳未连续推进")


def _write_stale_sidecar_fixture(runtime_root: Path) -> None:
    state_dir = runtime_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "game_overlay_sidecar_status.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build_id": "stale-build-before-migration",
                "status": "running",
                "phase": "loop",
                "pid": 2147483000,
                "pid_started_at": 1.0,
                "heartbeat_at": time.time(),
                "updated_at": time.time(),
                "data_generation_id": "stale-generation",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_populated_runtime_fixture(package_dir: Path, runtime_root: Path) -> None:
    """预装 bundle cohort 并加入 4 个 legacy/3 个损坏代，复现真实多代启动。"""

    data_root = _packaged_data_root(package_dir)
    manifest = _read_json_file(data_root / "bundle_manifest.json")
    prefix = "resources/seeds/runtime.v4/"
    for raw in manifest.get("seed_files", []):
        relative = str(raw).replace("\\", "/")
        if not relative.startswith(prefix):
            raise SmokeFailure("seed file outside v4 runtime prefix")
        local = Path(relative.removeprefix(prefix))
        if local.is_absolute() or ".." in local.parts:
            raise SmokeFailure("seed path escapes isolated runtime")
        source = data_root / relative
        target = runtime_root / local
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    generations = runtime_root / "snapshots" / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    for index in range(4):
        legacy = generations / f"2026081{index}T000000-legacy{index:02d}"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "generation_id": legacy.name,
                    "created_at": f"2026-08-1{index}T00:00:00+00:00",
                    "source_files": [
                        {"source": "hextech", "artifact_role": "stats"},
                        {"source": "catalog", "artifact_role": "augments"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    for index in range(3):
        broken = generations / f"2026082{index}T000000-broken{index:02d}"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "manifest.json").write_text("{broken", encoding="utf-8")


def _required_paths_ready(package_dir: Path, runtime_root: Path, started_at_wall: float) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    packaged_data_root = _packaged_data_root(package_dir)
    for rel in REQUIRED_PACKAGE_DIRS:
        checks[f"package:{rel}"] = (packaged_data_root / rel).is_dir()
    if _has_verified_snapshot_seed(package_dir):
        checks["package:resources/seeds/runtime.v4/snapshots/current.v4.json"] = (
            packaged_data_root / "resources" / "seeds" / "runtime.v4" / "snapshots" / "current.v4.json"
        ).is_file()
    if _has_verified_cohort_seed(package_dir):
        checks["package:v4_cohort_head"] = (
            packaged_data_root / "resources/seeds/runtime.v4/snapshots/current.v4.json"
        ).is_file()
    for rel in REQUIRED_RUNTIME_DIRS:
        checks[f"runtime:{rel}"] = (runtime_root / rel).is_dir()
    for rel in REQUIRED_RUNTIME_FILES:
        path = runtime_root / rel
        checks[f"runtime:{rel}"] = path.is_file() and path.stat().st_mtime >= started_at_wall
    checks["runtime:snapshots/current.v4.json"] = (runtime_root / "snapshots" / "current.v4.json").is_file()
    package_roots = [("package", package_dir)]
    if packaged_data_root != package_dir:
        package_roots.append(("_internal", packaged_data_root))
    for label, root in package_roots:
        for rel in FORBIDDEN_PACKAGE_PATHS:
            if rel == "var" and root == package_dir and runtime_root.resolve() == (package_dir / "var").resolve():
                continue
            checks[f"{label}:{rel} absent"] = not (root / Path(rel)).exists()
        checks[f"{label}:__pycache__ absent"] = not any(
            path.is_dir() and path.name == "__pycache__"
            for path in root.rglob("__pycache__")
        )
        checks[f"{label}:pyc/pyo absent"] = not any(
            path.is_file() and path.suffix.lower() in FORBIDDEN_PACKAGE_GENERATED_SUFFIXES
            for path in root.rglob("*")
        )
    checks["runtime:data absent"] = not (runtime_root / "data").exists()
    checks[f"package:{OVERLAY_ANCHOR_CALIBRATION_FILENAME} absent"] = not any(
        path.name == OVERLAY_ANCHOR_CALIBRATION_FILENAME
        for path in package_dir.rglob(OVERLAY_ANCHOR_CALIBRATION_FILENAME)
    )
    return checks


def _overlay_self_check(exe: Path, package_dir: Path, env: dict[str, str]) -> dict[str, object]:
    """在打包目录执行无 GUI 合同自检；它不能证明窗口或像素已呈现。"""

    with TemporaryDirectory(prefix="hextech-overlay-self-check-") as tmp:
        result_path = Path(tmp) / "result.json"
        token = secrets.token_urlsafe(24)
        child_env = dict(env)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_FILE"] = str(result_path)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"] = token
        completed = subprocess.run(
            [str(exe), "--game-overlay", "--self-check"],
            cwd=str(package_dir),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        output = completed.stdout.decode("utf-8", errors="replace")
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure(f"Overlay self-check 文件结果无效：{output[-800:]}") from exc
    if not isinstance(payload, dict) or not secrets.compare_digest(str(payload.get("token") or ""), token):
        raise SmokeFailure("Overlay self-check 文件结果 token 不匹配")
    payload = dict(payload)
    payload.pop("token", None)
    required = (
        "ok",
        "window_probe_ok",
        "context_contract_ok",
        "visibility_contract_ok",
        "lcu_scanner_configured",
        "overlay_event_contract_ok",
        "sidecar_status_contract_ok",
        "session_report_contract_ok",
    )
    if completed.returncode != 0 or not isinstance(payload, dict) or not all(payload.get(key) is True for key in required):
        raise SmokeFailure(f"Overlay self-check 失败：code={completed.returncode} payload={payload}")
    if payload.get("runtime_contracts") != RUNTIME_CONTRACT_VERSIONS:
        raise SmokeFailure(f"Overlay self-check 运行契约不匹配：{payload.get('runtime_contracts')}")
    if str(payload.get("build_id") or "") != str(env.get("HEXTECH_EXPECTED_BUILD_ID") or payload.get("build_id") or ""):
        raise SmokeFailure("Overlay self-check build_id 与候选不一致")
    return payload


def _validate_desktop_presentation_report(payload: dict, *, expected_build: str) -> None:
    """拒绝旧owner模型、空控件/像素、伪成功或不匹配的冻结身份。"""
    controls = payload.get("controls")
    pixels = payload.get("pixels")
    layer = payload.get("layer")
    footprint = payload.get("footprint")
    fixture = payload.get("fixture")
    rect = payload.get("actual_rect")
    blockers = payload.get("blockers")
    hide_ms = payload.get("hide_ms")
    first_show_ms = payload.get("first_show_ms")
    required_controls = ("title_bar", "refresh_button", "diagnostics_button", "canvas", "status_line_label")
    expected_pixels = {"canvas": [9, 20, 40], "title_frame": [10, 20, 40]}
    checks = {
        "state": payload.get("state") == "ok",
        "build_id": bool(expected_build) and payload.get("build_id") == expected_build,
        "contract": payload.get("runtime_contract") == "desktop-independent-panel-v1",
        "marker": payload.get("footprint_marker") == "hextech-desktop-presentation-smoke-v1",
        "no_external_owner": payload.get("no_external_owner") is True
        and type(payload.get("owner_hwnd")) is int and payload["owner_hwnd"] == 0,
        "mapped_opaque": payload.get("mapped") is True and payload.get("alpha") == 1.0,
        "actual_rect": isinstance(rect, list) and len(rect) == 4
        and all(type(value) is int for value in rect) and rect[2] > rect[0] and rect[3] > rect[1],
        "controls": isinstance(controls, dict) and all(
            isinstance(controls.get(name), dict) and controls[name].get("mapped") is True
            and type(controls[name].get("width")) is int and controls[name]["width"] > 0
            and type(controls[name].get("height")) is int and controls[name]["height"] > 0
            for name in required_controls
        ),
        "pixels": isinstance(pixels, dict) and all(
            isinstance(pixels.get(name), dict) and pixels[name].get("rgb") == expected
            and pixels[name].get("expected_rgb") == expected for name, expected in expected_pixels.items()
        ),
        "independent_layer": isinstance(layer, dict) and layer.get("native_owner") == 0
        and layer.get("independent_window") is True and layer.get("desired_topmost") is True
        and layer.get("actual_topmost") is True and layer.get("client_foreground") is True,
        "blockers": isinstance(blockers, list) and len(blockers) == 2 and all(
            isinstance(case, dict) and case.get("panel_above") is True
            and case.get("topmost") is topmost for case, topmost in zip(blockers, (False, True))
        ),
        "hide_budget": isinstance(hide_ms, (int, float)) and not isinstance(hide_ms, bool)
        and 0 <= hide_ms <= 100,
        "first_show_budget": isinstance(first_show_ms, (int, float)) and not isinstance(first_show_ms, bool)
        and 0 <= first_show_ms <= 300,
        "foreground": payload.get("foreground_unchanged") is True,
        "resources": payload.get("resources_closed") is True,
        "fixture_scope": isinstance(fixture, dict) and fixture.get("kind") == "self_owned_RCLIENT_like"
        and fixture.get("real_league_acceptance") is False,
        "footprint": isinstance(footprint, dict) and all(
            type(footprint.get(name)) is int and footprint[name] == 0 for name in
            ("network_requests", "service_processes", "avatar_cache_writes", "screenshots_saved", "external_window_mutations")
        ),
    }
    failed = [name for name, success in checks.items() if not success]
    if failed:
        raise SmokeFailure("Desktop presentation smoke 失败：" + ", ".join(failed))


def _desktop_presentation_smoke(exe: Path, package_dir: Path, env: dict[str, str]) -> dict[str, object]:
    """独立EXE走实际HextechUI；bootstrap文件是无控制台进程的权威结果。"""
    with TemporaryDirectory(prefix="hextech-desktop-presentation-smoke-") as tmp:
        result_path = Path(tmp) / "result.json"
        token = secrets.token_urlsafe(24)
        child_env = dict(env)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_FILE"] = str(result_path)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"] = token
        completed = subprocess.run(
            [str(exe.resolve()), "--desktop-presentation-smoke"], cwd=str(package_dir.resolve()),
            env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False,
        )
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure("Desktop presentation smoke 文件结果无效") from exc
    if not isinstance(payload, dict) or not secrets.compare_digest(str(payload.get("token") or ""), token):
        raise SmokeFailure("Desktop presentation smoke 文件结果 token 不匹配")
    payload.pop("token", None)
    if completed.returncode != 0:
        raise SmokeFailure(f"Desktop presentation smoke 进程失败：code={completed.returncode} "
                           f"reason={payload.get('reason', '')}")
    _validate_desktop_presentation_report(payload, expected_build=str(env.get("HEXTECH_EXPECTED_BUILD_ID") or ""))
    return payload


def _overlay_presentation_smoke(
    exe: Path,
    package_dir: Path,
    env: dict[str, str],
) -> dict[str, object]:
    """运行真实 Tk/Win32/像素探针；无 GUI self-check 不能替代本门。"""

    with TemporaryDirectory(prefix="hextech-overlay-presentation-smoke-") as tmp:
        result_path = Path(tmp) / "result.json"
        token = secrets.token_urlsafe(24)
        child_env = dict(env)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_FILE"] = str(result_path)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"] = token
        completed = subprocess.run(
            [str(exe), "--game-overlay", "--presentation-smoke"],
            cwd=str(package_dir),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        output = completed.stdout.decode("utf-8", errors="replace")
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure(f"Overlay presentation smoke 文件结果无效：{output[-800:]}") from exc
    if not isinstance(payload, dict) or not secrets.compare_digest(str(payload.get("token") or ""), token):
        raise SmokeFailure("Overlay presentation smoke 文件结果 token 不匹配")
    payload = dict(payload)
    payload.pop("token", None)
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    presentation = (
        payload.get("presentation") if isinstance(payload.get("presentation"), dict) else {}
    )
    capture_exclusion_probe = (
        payload.get("capture_exclusion_probe")
        if isinstance(payload.get("capture_exclusion_probe"), dict)
        else {}
    )
    if (
        completed.returncode != 0
        or payload.get("ok") is not True
        or not checks
        or not all(checks.values())
        or presentation.get("state") != "composed"
        or not isinstance(presentation.get("composition_probe"), dict)
        or presentation["composition_probe"].get("state") != "excluded"
        or capture_exclusion_probe.get("state") != "excluded"
        or capture_exclusion_probe.get("probe_contract") != "mss_capture_exclusion_v1"
        or capture_exclusion_probe.get("background_matched_count") != capture_exclusion_probe.get("sample_count")
    ):
        raise SmokeFailure(
            f"Overlay presentation smoke 失败：code={completed.returncode} payload={payload}"
        )
    if str(payload.get("build_id") or "") != str(
        env.get("HEXTECH_EXPECTED_BUILD_ID") or payload.get("build_id") or ""
    ):
        raise SmokeFailure("Overlay presentation smoke build_id 与候选不一致")
    return payload


def _diagnostic_retention_smoke(
    exe: Path,
    package_dir: Path,
    env: dict[str, str],
) -> dict[str, object]:
    """在冻结 Sidecar 内验证 20 个 v1 不挤占 v2 及连续两轮留存。"""

    with TemporaryDirectory(prefix="hextech-diagnostic-retention-smoke-") as tmp:
        result_path = Path(tmp) / "result.json"
        token = secrets.token_urlsafe(24)
        child_env = dict(env)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_FILE"] = str(result_path)
        child_env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"] = token
        completed = subprocess.run(
            [str(exe), "--overlay-sidecar", "--diagnostic-retention-smoke"],
            cwd=str(package_dir),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        output = completed.stdout.decode("utf-8", errors="replace")
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure(f"Diagnostic retention smoke 文件结果无效：{output[-800:]}") from exc
    if not isinstance(payload, dict) or not secrets.compare_digest(str(payload.get("token") or ""), token):
        raise SmokeFailure("Diagnostic retention smoke 文件结果 token 不匹配")
    payload = dict(payload)
    payload.pop("token", None)
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    if (
        completed.returncode != 0
        or payload.get("ok") is not True
        or not checks
        or not all(checks.values())
        or str(payload.get("build_id") or "") != str(env.get("HEXTECH_EXPECTED_BUILD_ID") or "")
    ):
        raise SmokeFailure(
            f"Diagnostic retention smoke 失败：code={completed.returncode} payload={payload}"
        )
    return payload


def _terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _sidecar_pool_smoke(
    exe: Path,
    package_dir: Path,
    child_env: dict[str, str],
    runtime_root: Path,
    bundle_manifest: dict[str, object],
) -> dict[str, object]:
    """在隔离运行态完成生产池模板构建；无游戏窗口不视为失败。"""

    cohort = bundle_manifest.get("cohort_seed")
    if not isinstance(cohort, dict) or not cohort:
        return {"state": "not_applicable"}
    from tooling.build.recognition_contract import recognition_pool_contract

    try:
        recognition = recognition_pool_contract(runtime_root, cohort)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SmokeFailure(f"Sidecar recognition Catalog 无效：{type(exc).__name__}: {exc}") from exc
    status_path = runtime_root / "state" / "game_overlay_sidecar_status.json"
    status_path.unlink(missing_ok=True)
    env = dict(child_env)
    env["HEXTECH_OVERLAY_SIDECAR_DEBUG_DUMP"] = "0"
    completed = subprocess.run(
        [
            str(exe.resolve()),
            "--overlay-sidecar",
            "--once",
            "--required-frames",
            "1",
            "--frame-interval-ms",
            "0",
        ],
        cwd=str(package_dir.resolve()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"Sidecar pool smoke 状态不可读：{type(exc).__name__}") from exc
    if not isinstance(status, dict):
        raise SmokeFailure("Sidecar pool smoke 状态必须是对象")
    expected_pool_count = int(recognition["production_pool_count"])
    from hextech.modules.acquisition.hextech.production_pool import production_capability_status_valid

    matrix_rows = status.get("matrix_rows")
    excluded = status.get("excluded_reason_counts")
    checks = {
        "returncode": completed.returncode == 0,
        "schema_version": int(status.get("schema_version") or 0) == 2,
        "build_id": str(status.get("build_id") or "") == str(bundle_manifest.get("build_id") or ""),
        "status": status.get("status") == "stopped",
        "phase": status.get("phase") == "once_complete",
        "vision_pool_generation": str(
            status.get("vision_pool_generation_id") or status.get("data_generation_id") or ""
        ) == str(cohort.get("generation_id") or ""),
        "generation_roles": str(status.get("stats_generation_id") or "") == ""
        and isinstance(status.get("generation_roles"), dict),
        "catalog_generation": str(status.get("catalog_generation_id") or "")
        == recognition["catalog_generation_id"],
        "recognition_catalog": str(status.get("recognition_catalog_id") or "")
        == recognition["recognition_catalog_id"],
        "pool_id": str(status.get("production_pool_id") or "") == recognition["production_pool_id"],
        "pool_state": status.get("production_pool_state") == "ready",
        "pool_count": int(status.get("production_pool_count") or 0) == expected_pool_count,
        "full_catalog_count": int(status.get("full_catalog_count") or 0)
        == recognition["full_catalog_count"],
        "rank_identity_count": int(status.get("rank_identity_count") or 0) == expected_pool_count,
        "matrix_rows": production_capability_status_valid(status, expected_pool_count)
        if status.get("production_pool_schema_version") == 2 else isinstance(matrix_rows, dict)
        and all(int(matrix_rows.get(channel) or 0) >= expected_pool_count for channel in ("icon", "name", "alt_name"))
        and int(matrix_rows.get("observed_name") or 0) > 0,
        "no_unresolved": isinstance(excluded, dict)
        and all(int(excluded.get(reason) or 0) == 0 for reason in ("unresolved", "duplicate", "name_conflict")),
    }
    if not all(checks.values()):
        failed = [name for name, ok in checks.items() if not ok]
        output = completed.stdout.decode("utf-8", errors="replace")[-1000:]
        raise SmokeFailure(f"Sidecar pool smoke 失败：{', '.join(failed)} output={output}")
    return {
        "state": "ready",
        "production_pool_id": str(status.get("production_pool_id") or ""),
        "production_pool_count": int(status.get("production_pool_count") or 0),
        "full_catalog_count": int(status.get("full_catalog_count") or 0),
        "rank_identity_count": int(status.get("rank_identity_count") or 0),
        "matrix_rows": matrix_rows,
        "excluded_reason_counts": excluded,
    }


def _acquisition_worker_import_smoke(
    exe: Path,
    package_dir: Path,
    child_env: dict[str, str],
    runtime_root: Path,
    bundle_manifest: dict[str, object],
) -> dict[str, object]:
    """无网络验证冻结 worker 与 curl_cffi 原生 DLL 可从实际路径导入。"""

    result_path = runtime_root / "state" / "acquisition_worker_self_check.v1.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.unlink(missing_ok=True)
    completed = subprocess.run(
        [
            str(exe.resolve()),
            "--acquisition-worker",
            "--self-check",
            "--result-output",
            str(result_path.resolve()),
        ],
        cwd=str(package_dir.resolve()),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        output = completed.stdout.decode("utf-8", errors="replace")[-1000:]
        raise SmokeFailure(
            f"Acquisition worker self-check 结果不可读：{type(exc).__name__} output={output}"
        ) from exc
    checks = payload.get("checks") if isinstance(payload, dict) else None
    required = {
        "curl_cffi._wrapper",
        "hextech.infrastructure.sources.aramkit.service",
        "hextech.infrastructure.sources.catalog_versioned",
    }
    valid = bool(
        completed.returncode == 0
        and isinstance(payload, dict)
        and payload.get("state") == "ready"
        and str(payload.get("build_id") or "") == str(bundle_manifest.get("build_id") or "")
        and isinstance(checks, dict)
        and required <= set(checks)
        and all(checks.get(name) == "ready" for name in required)
    )
    if not valid:
        reason = str(payload.get("reason_code") or payload.get("error_type") or "self_check_failed")
        raise SmokeFailure(f"Acquisition worker self-check 失败：{reason}")
    if bundle_manifest.get("runtime_profile") == "isolated_candidate":
        actual_root = str(payload.get("runtime_root") or "").strip()
        if not actual_root or Path(actual_root).resolve() != runtime_root.resolve():
            raise SmokeFailure("Acquisition worker 实际 runtime_root 与候选包 var 不一致")
        if payload.get("runtime_profile") != "isolated_candidate":
            raise SmokeFailure("Acquisition worker 实际 runtime_profile 与候选不一致")
    return dict(payload)


def run_smoke(
    package_dir: Path,
    timeout_seconds: int,
    *,
    fixture: str = "clean",
) -> dict[str, object]:
    bundle_manifest = _validate_bundle_contract(package_dir)
    exe = _find_exe(package_dir)
    conflict = _runtime_environment_conflict(exe)
    if conflict:
        return {"ok": False, "fixture": fixture, "package_dir": str(package_dir),
                "blocked_reason": conflict, "last_error": conflict}
    # 覆盖所有子进程与失败出口；正式 runtime 从父环境定位，绝不从 sandbox 推导。
    protected_root = _get_packaged_runtime_root(os.environ).resolve()
    before = _runtime_metadata_snapshot(protected_root)
    try:
        result = _run_smoke_fixture(package_dir, timeout_seconds, fixture=fixture, bundle_manifest=bundle_manifest)
    except (OSError, subprocess.TimeoutExpired, SmokeFailure) as exc:
        result = {"ok": False, "fixture": fixture, "package_dir": str(package_dir), "last_error": str(exc)}
    after = _runtime_metadata_snapshot(protected_root)
    changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    result["formal_runtime_unchanged"] = not changed
    result["formal_runtime_metadata"] = {
        "root": str(protected_root), "before_entries": len(before), "after_entries": len(after),
        "changed_paths": changed,
    }
    if changed:
        result.update(ok=False, last_error="正式 runtime 元数据在 smoke 期间发生变化")
    return result


def _run_smoke_fixture(
    package_dir: Path, timeout_seconds: int, *, fixture: str, bundle_manifest: dict[str, object],
) -> dict[str, object]:
    exe = _find_exe(package_dir)
    _find_launcher(package_dir)
    _validate_windows_gui_subsystem(exe)
    overall_started_at = time.monotonic()
    child_env, runtime_root = _smoke_runtime_environment(package_dir, fixture, bundle_manifest)
    if runtime_root.exists():
        raise SmokeFailure("fixture runtime 已存在；请使用新的独立包副本/隔离目录")
    child_env["HEXTECH_APP_SKIP_AUTO_REFRESH"] = "1"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["HEXTECH_LAUNCHER_WAIT"] = "1"
    child_env["HEXTECH_EXPECTED_BUILD_ID"] = str(bundle_manifest.get("build_id") or "")
    verified_snapshot_seeded = _has_verified_snapshot_seed(package_dir)
    stdout_path = runtime_root / "logs" / f"smoke_startup_{fixture}_stdout.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    _write_smoke_feature_flags(runtime_root)
    if fixture == "stale_sidecar":
        _write_stale_sidecar_fixture(runtime_root)
    elif fixture == "populated_runtime":
        _write_populated_runtime_fixture(package_dir, runtime_root)
    elif fixture != "clean":
        raise SmokeFailure(f"未知 packaged smoke fixture：{fixture}")
    try:
        acquisition_worker_self_check = _acquisition_worker_import_smoke(
            exe,
            package_dir,
            child_env,
            runtime_root,
            bundle_manifest,
        )
    except (OSError, subprocess.TimeoutExpired, SmokeFailure) as exc:
        return {
            "ok": False,
            "elapsed_seconds": round(time.monotonic() - overall_started_at, 2),
            "package_dir": str(package_dir),
            "runtime_root": str(runtime_root),
            "fixture": fixture,
            "verified_snapshot_seeded": verified_snapshot_seeded,
            "acquisition_worker_self_check": {},
            "overlay_self_check": {},
            "desktop_presentation_smoke": {},
            "overlay_presentation_smoke": {},
            "last_error": str(exc),
        }
    # clean/stale fixture 必须先让唯一 Desktop owner 安装 seed；Host self-check
    # 在 full chain 回收后再运行，不能在 Desktop 之前伪造第二个 seed owner。
    overlay_self_check: dict[str, object] = {}
    desktop_presentation_smoke: dict[str, object] = {}
    try:
        desktop_presentation_smoke = _desktop_presentation_smoke(exe, package_dir, child_env)
        overlay_presentation_smoke = _overlay_presentation_smoke(exe, package_dir, child_env)
    except (OSError, subprocess.TimeoutExpired, SmokeFailure) as exc:
        return {
            "ok": False,
            "elapsed_seconds": round(time.monotonic() - overall_started_at, 2),
            "package_dir": str(package_dir),
            "runtime_root": str(runtime_root),
            "fixture": fixture,
            "verified_snapshot_seeded": verified_snapshot_seeded,
            "acquisition_worker_self_check": acquisition_worker_self_check,
            "overlay_self_check": overlay_self_check,
            "desktop_presentation_smoke": desktop_presentation_smoke,
            "overlay_presentation_smoke": {},
            "last_error": str(exc),
        }

    started_at = time.monotonic()
    started_at_wall = time.time()
    with stdout_path.open("wb") as stdout:
        # smoke 直接启动 EXE，和桌面快捷方式一致；BAT 仅保留便携包人工入口。
        command = [str(exe.resolve())]
        proc = subprocess.Popen(
            command,
            cwd=str(package_dir.resolve()),
            stdout=stdout,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
    try:
        last_error = ""
        checks: dict[str, bool] = {}
        chain_checks: dict[str, bool] = {}
        chain: dict[str, object] = {}
        sidecar_pool: dict[str, object] = {}
        chain_ready = False
        while time.monotonic() - started_at < timeout_seconds:
            conflict = _runtime_environment_conflict(exe)
            if conflict:
                last_error = conflict
                break
            checks = _required_paths_ready(package_dir, runtime_root, started_at_wall)
            if all(checks.values()):
                chain_checks, chain = _overlay_chain_status(
                    runtime_root,
                    bundle_manifest,
                    desktop_pid=int(getattr(proc, "pid", 0) or 0),
                    started_at_wall=started_at_wall,
                )
                if all(chain_checks.values()):
                    try:
                        chain["heartbeats"] = _wait_for_overlay_heartbeats(
                            runtime_root,
                            host_pid=int(chain.get("host_pid") or 0),
                            sidecar_pid=int(chain.get("sidecar_pid") or 0),
                            host_updated_at=float(chain.get("host_updated_at") or 0.0),
                            sidecar_heartbeat_at=float(chain.get("sidecar_heartbeat_at") or 0.0),
                        )
                        chain_ready = True
                        break
                    except SmokeFailure as exc:
                        last_error = str(exc)
                else:
                    missing = [name for name, ok in chain_checks.items() if not ok]
                    last_error = "完整 Overlay 链未就绪：" + ", ".join(missing)
            if proc.poll() is not None:
                last_error = f"进程提前退出：returncode={proc.returncode}"
                break
            time.sleep(1)
    finally:
        _terminate_process_tree(proc)

    base_result: dict[str, object] = {
        "ok": False,
        "elapsed_seconds": round(time.monotonic() - overall_started_at, 2),
        "package_dir": str(package_dir),
        "build_id": str(bundle_manifest.get("build_id") or ""),
        "runtime_root": str(runtime_root),
        "fixture": fixture,
        "verified_snapshot_seeded": verified_snapshot_seeded,
        "acquisition_worker_self_check": acquisition_worker_self_check,
        "overlay_self_check": overlay_self_check,
        "desktop_presentation_smoke": desktop_presentation_smoke,
        "overlay_presentation_smoke": overlay_presentation_smoke,
        "sidecar_pool": sidecar_pool,
        "paths": checks,
        "overlay_chain_checks": chain_checks,
        "overlay_chain": chain,
        "last_error": last_error,
        "stdout_tail": stdout_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        if stdout_path.exists()
        else "",
    }
    if not chain_ready:
        return base_result
    try:
        overlay_self_check = _overlay_self_check(exe, package_dir, child_env)
        base_result["overlay_self_check"] = overlay_self_check
    except (OSError, subprocess.TimeoutExpired, SmokeFailure) as exc:
        return {**base_result, "last_error": str(exc)}
    try:
        # 与 full chain 串行，避免独立 --once 争用正在运行的 Sidecar 锁。
        sidecar_pool = _sidecar_pool_smoke(
            exe,
            package_dir,
            child_env,
            runtime_root,
            bundle_manifest,
        )
    except (OSError, subprocess.TimeoutExpired, SmokeFailure) as exc:
        return {**base_result, "last_error": str(exc)}
    try:
        diagnostic_retention = _diagnostic_retention_smoke(
            exe,
            package_dir,
            child_env,
        )
    except (OSError, subprocess.TimeoutExpired, SmokeFailure) as exc:
        return {**base_result, "sidecar_pool": sidecar_pool, "last_error": str(exc)}
    return {
        **base_result,
        "ok": True,
        "elapsed_seconds": round(time.monotonic() - overall_started_at, 2),
        "sidecar_pool": sidecar_pool,
        "diagnostic_retention": diagnostic_retention,
        "last_error": "",
    }


def _runtime_environment_conflict(expected_exe: Path) -> str:
    """Native fixtures must not compete with a user's live game or another runtime.

    Only process names and the matching app's executable path are inspected;
    command lines, credentials and external window contents are never read.
    """
    import psutil
    expected = str(expected_exe.resolve()).casefold()
    try:
        for process in psutil.process_iter(["name"]):
            name = str(process.info.get("name") or "").casefold()
            if name == "league of legends.exe":
                return "real_game_active_smoke_requires_idle_environment"
            if name == "hextech伴生终端.exe":
                try:
                    if str(Path(process.exe()).resolve()).casefold() != expected:
                        return "other_hextech_runtime_active"
                except psutil.NoSuchProcess:
                    continue
                except psutil.Error:
                    return "other_hextech_runtime_identity_unknown"
    except psutil.Error:
        return "runtime_environment_probe_failed"
    return ""


def main() -> int:
    reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure_stdout):
        reconfigure_stdout(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="验证打包产物空仓首启是否在限定时间内可用。")
    default_releases = Path(__file__).resolve().parents[3] / ".artifacts" / "hextech" / "releases"
    parser.add_argument("--package-dir", type=Path, help="已打包便携目录；默认使用 .artifacts/hextech/releases 下最新目录。")
    parser.add_argument("--dist-dir", type=Path, default=default_releases, help="便携包搜索根目录；默认是 .artifacts/hextech/releases。")
    parser.add_argument("--smoke-root", type=Path, default=Path(__file__).resolve().parents[2] / ".tmp_package_smoke", help="烟测复制副本根目录；默认是 run/.tmp_package_smoke。")
    parser.add_argument("--timeout", type=int, default=210, help="单个完整启动链等待秒数；默认 210。")
    parser.add_argument("--keep", action="store_true", help="保留复制出的烟测目录，便于排查。")
    args = parser.parse_args()

    source = args.package_dir or _latest_package(args.dist_dir)
    smoke_root = args.smoke_root.resolve()
    if smoke_root.exists() and any(smoke_root.iterdir()):
        raise SmokeFailure("smoke root 已有证据；请使用新的隔离目录")
    smoke_root.mkdir(parents=True, exist_ok=True)
    fixture_results: dict[str, dict[str, object]] = {}
    for fixture in ("clean", "stale_sidecar", "populated_runtime"):
        target = _copy_clean_package(source.resolve(), smoke_root / fixture)
        fixture_results[fixture] = run_smoke(target, args.timeout, fixture=fixture)
    result: dict[str, object] = {
        "ok": all(item.get("ok") is True for item in fixture_results.values()),
        "package_dir": str(smoke_root),
        "fixtures": fixture_results,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["ok"] and not args.keep:
        if not _cleanup_smoke_root(smoke_root):
            raise SmokeFailure(f"烟测进程退出后仍无法清理隔离目录：{smoke_root}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
