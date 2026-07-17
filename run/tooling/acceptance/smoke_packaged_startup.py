"""打包产物空仓首启烟测。

这个文件用于验证 PyInstaller 便携包在非仓库空目录首次启动时，是否能在限定时间内创建运行态目录、启动本地 Web 服务并返回可操作页面。
它只负责本地验收，不负责构建产物、不负责真实人工点击悬浮窗、不修改业务数据。

调用方: dev_checks; 关键依赖: 见 imports。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


REQUIRED_PACKAGE_DIRS = (
    "resources/catalog",
    "resources/assets",
    "resources/seeds/generations",
)

REQUIRED_RUNTIME_DIRS = (
    "state",
    "locks",
    "profiles",
    "snapshots",
)

REQUIRED_RUNTIME_FILES = (
    "state/web_server_port.txt",
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
    "web_frontend_enabled": True,
    "game_overlay_enabled": False,
    "auto_open_browser": False,
    "private_policy_stats_enabled": False,
    "low_frequency_listener_enabled": True,
}


class SmokeFailure(RuntimeError):
    pass


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
    if smoke_root.exists():
        shutil.rmtree(smoke_root)
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
    source_env = env or os.environ
    local_app_data = source_env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "HextechNexus" / "var"
    app_data = source_env.get("APPDATA", "").strip()
    if app_data:
        return Path(app_data) / "HextechNexus" / "var"
    return Path.home() / ".hextech_nexus" / "var"


def _read_port(runtime_root: Path) -> str | None:
    port_file = runtime_root / "state/web_server_port.txt"
    if not port_file.exists():
        return None
    port = port_file.read_text(encoding="utf-8", errors="replace").strip()
    return port or None


def _packaged_data_root(package_dir: Path) -> Path:
    return package_dir / "_internal" if (package_dir / "_internal").exists() else package_dir


def _has_verified_snapshot_seed(package_dir: Path) -> bool:
    return (_packaged_data_root(package_dir) / "resources" / "seeds" / "current.v1.json").is_file()


def _write_smoke_feature_flags(runtime_root: Path) -> None:
    """烟测显式打开 Web 热路径，避免被用户默认双开关关闭语义影响。"""

    state_dir = runtime_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "ui_feature_flags.json").write_text(
        json.dumps(SMOKE_FEATURE_FLAGS, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fetch(url: str, timeout: float = 8.0, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _required_paths_ready(package_dir: Path, runtime_root: Path, started_at_wall: float) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    packaged_data_root = _packaged_data_root(package_dir)
    for rel in REQUIRED_PACKAGE_DIRS:
        checks[f"package:{rel}"] = (packaged_data_root / rel).is_dir()
    if _has_verified_snapshot_seed(package_dir):
        checks["package:resources/seeds/current.v1.json"] = (
            packaged_data_root / "resources" / "seeds" / "current.v1.json"
        ).is_file()
    for rel in REQUIRED_RUNTIME_DIRS:
        checks[f"runtime:{rel}"] = (runtime_root / rel).is_dir()
    for rel in REQUIRED_RUNTIME_FILES:
        path = runtime_root / rel
        checks[f"runtime:{rel}"] = path.is_file() and path.stat().st_mtime >= started_at_wall
    checks["runtime:snapshots/current.v1.json"] = (runtime_root / "snapshots" / "current.v1.json").is_file()
    package_roots = [("package", package_dir)]
    if packaged_data_root != package_dir:
        package_roots.append(("_internal", packaged_data_root))
    for label, root in package_roots:
        for rel in FORBIDDEN_PACKAGE_PATHS:
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


def _read_json(body: bytes) -> object:
    return json.loads(body.decode("utf-8", errors="replace"))


def _truthy_status(payload: dict[str, object], *keys: str) -> bool:
    return any(bool(payload.get(key)) for key in keys)


def _first_present(mapping: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _compact_hextech_card(card: object) -> dict[str, object]:
    if not isinstance(card, dict):
        return {}
    return {
        "id": _first_present(card, ("id", "海克斯ID", "augment_id", "augmentId")),
        "name": _first_present(card, ("name", "海克斯名称", "augment_name", "augmentName")),
        "win_rate": card.get("海克斯胜率", card.get("win_rate", card.get("winrate"))),
        "pick_rate": card.get("海克斯出场率", card.get("pick_rate", card.get("pickrate"))),
    }


def _extract_representative_champion(champions: object) -> tuple[str, str]:
    """从 `/api/champions` 真实 payload 中提取烟测代表英雄。"""

    if not isinstance(champions, list):
        return "", ""
    for champion in champions:
        if not isinstance(champion, dict):
            continue
        representative_name = _first_present(
            champion,
            ("英雄名称", "hero_name", "heroName", "name", "champion_name", "championName"),
        )
        representative_id = _first_present(
            champion,
            ("英雄 ID", "英雄ID", "hero_id", "heroId", "champion_id", "championId", "id"),
        )
        if representative_name or representative_id:
            return representative_name, representative_id
    return "", ""


def _business_ready(
    startup_status: object,
    champions: object,
    detail_payload: object,
    synergy_payload: object,
    representative_asset: object,
    *,
    require_snapshot_status: bool = False,
) -> dict[str, bool]:
    startup = startup_status if isinstance(startup_status, dict) else {}
    snapshot_value = startup.get("data_snapshot")
    snapshot = snapshot_value if isinstance(snapshot_value, dict) else {}
    champion_list = champions if isinstance(champions, list) else []
    detail = detail_payload if isinstance(detail_payload, dict) else {}
    synergy = synergy_payload if isinstance(synergy_payload, dict) else {}
    asset = representative_asset if isinstance(representative_asset, dict) else {}
    snapshot_generation = str(snapshot.get("generation_id") or "")
    detail_generation = str(detail.get("generation_id") or "")
    snapshot_ready = snapshot.get("state") in {"ready", "degraded"} and bool(snapshot_generation)
    return {
        "startup_status_reachable": bool(startup),
        "snapshot_generation_ready": snapshot_ready if require_snapshot_status else True,
        "web_generation_matches_snapshot": (
            bool(detail_generation) and detail_generation == snapshot_generation
            if require_snapshot_status
            else bool(detail_generation)
        ),
        "champions_non_empty": len(champion_list) > 0,
        "detail_user_visible": bool(detail.get("comprehensive")) or bool(detail.get("ready")) or bool(detail.get("loading")),
        "synergy_api_reachable": isinstance(synergy, dict),
        "synergy_payload_present": isinstance(synergy.get("synergies"), list),
        "representative_asset_reachable": asset.get("code") == 200 and int(asset.get("bytes") or 0) > 0,
    }


def _read_runtime_auth_token(runtime_root: Path) -> str:
    try:
        return (runtime_root / "state" / "auth_token.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _local_auth_headers(base: str, runtime_root: Path) -> dict[str, str]:
    token = _read_runtime_auth_token(runtime_root)
    return {"Origin": base, "X-Hextech-Token": token}


def _web_ready(port: str, runtime_root: Path, *, require_snapshot_status: bool = False) -> dict[str, object]:
    base = f"http://127.0.0.1:{port}"
    result: dict[str, object] = {}

    root_code, root_body = _fetch(base + "/")
    result["root"] = {"code": root_code, "bytes": len(root_body)}

    startup_code, startup_body = _fetch(base + "/api/startup_status", headers=_local_auth_headers(base, runtime_root))
    startup_status = _read_json(startup_body)
    result["startup_status"] = {"code": startup_code, "bytes": len(startup_body), "json": startup_status}

    champions_code, champions_body = _fetch(base + "/api/champions")
    champions = _read_json(champions_body)
    champion_sample_keys: list[str] = []
    if isinstance(champions, list) and champions and isinstance(champions[0], dict):
        champion_sample_keys = list(champions[0].keys())
    result["champions"] = {
        "code": champions_code,
        "bytes": len(champions_body),
        "count": len(champions) if isinstance(champions, list) else 0,
        "sample_keys": champion_sample_keys,
    }

    detail_code, detail_body = _fetch(base + "/detail.html?champion=1")
    result["detail"] = {"code": detail_code, "bytes": len(detail_body)}

    representative_name, representative_id = _extract_representative_champion(champions)
    result["representative"] = {"hero": representative_name, "hero_id": representative_id}
    detail_payload: object = {}
    if representative_name:
        api_detail_code, api_detail_body = _fetch(base + f"/api/champion/{urllib.parse.quote(representative_name)}/hextechs")
        detail_payload = _read_json(api_detail_body)
        cards = detail_payload.get("comprehensive") if isinstance(detail_payload, dict) else []
        result["representative_detail"] = {
            "code": api_detail_code,
            "bytes": len(api_detail_body),
            "hero": representative_name,
            "generation_id": str(detail_payload.get("generation_id") or "") if isinstance(detail_payload, dict) else "",
            "status": str(detail_payload.get("status") or "") if isinstance(detail_payload, dict) else "",
            "card_count": len(cards) if isinstance(cards, list) else 0,
            "first_card": _compact_hextech_card(cards[0]) if isinstance(cards, list) and cards else {},
        }
    else:
        result["representative_detail"] = {"code": 0, "bytes": 0, "hero": ""}

    if representative_id:
        synergy_code, synergy_body = _fetch(base + f"/api/synergies/{urllib.parse.quote(representative_id)}")
        synergy_payload = _read_json(synergy_body)
        result["synergy_fallback"] = {"code": synergy_code, "bytes": len(synergy_body), "hero_id": representative_id, "json": synergy_payload}
    else:
        synergy_payload = {}
        result["synergy_fallback"] = {"code": 0, "bytes": 0, "hero_id": "", "json": synergy_payload}

    if representative_id:
        asset_code, asset_body = _fetch(base + f"/assets/champions/{urllib.parse.quote(representative_id)}.png")
        representative_asset = {"code": asset_code, "bytes": len(asset_body), "hero_id": representative_id}
    else:
        representative_asset = {"code": 0, "bytes": 0, "hero_id": ""}
    result["representative_asset"] = representative_asset

    business_checks = _business_ready(
        startup_status,
        champions,
        detail_payload,
        synergy_payload,
        representative_asset,
        require_snapshot_status=require_snapshot_status,
    )
    result["business_ready"] = business_checks
    if not all(business_checks.values()):
        missing = [name for name, ok in business_checks.items() if not ok]
        raise SmokeFailure("业务数据未就绪：" + ", ".join(missing))
    return result


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


def run_smoke(package_dir: Path, timeout_seconds: int) -> dict[str, object]:
    exe = _find_exe(package_dir)
    launcher = _find_launcher(package_dir)
    stdout_path = package_dir / "smoke_startup_stdout.log"
    started_at = time.monotonic()
    started_at_wall = time.time()
    child_env = os.environ.copy()
    appdata_root = package_dir.parent / "appdata"
    child_env["LOCALAPPDATA"] = str(appdata_root / "Local")
    child_env["APPDATA"] = str(appdata_root / "Roaming")
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["HEXTECH_LAUNCHER_WAIT"] = "1"
    verified_snapshot_seeded = _has_verified_snapshot_seed(package_dir)
    if verified_snapshot_seeded:
        child_env["HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH"] = "1"
    runtime_root = _get_packaged_runtime_root(child_env)
    _write_smoke_feature_flags(runtime_root)
    with stdout_path.open("wb") as stdout:
        command = ["cmd.exe", "/d", "/c", str(launcher.resolve())] if os.name == "nt" else [str(exe.resolve())]
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
        web: dict[str, object] = {}
        while time.monotonic() - started_at < timeout_seconds:
            checks = _required_paths_ready(package_dir, runtime_root, started_at_wall)
            port = _read_port(runtime_root)
            if port and all(checks.values()):
                try:
                    web = _web_ready(port, runtime_root, require_snapshot_status=verified_snapshot_seeded)
                    elapsed = time.monotonic() - started_at
                    return {
                        "ok": True,
                        "elapsed_seconds": round(elapsed, 2),
                        "package_dir": str(package_dir),
                        "runtime_root": str(runtime_root),
                        "port": port,
                        "verified_snapshot_seeded": verified_snapshot_seeded,
                        "paths": checks,
                        "web": web,
                    }
                except (urllib.error.URLError, TimeoutError, OSError, SmokeFailure, json.JSONDecodeError) as exc:
                    last_error = repr(exc)
            if proc.poll() is not None:
                last_error = f"进程提前退出：returncode={proc.returncode}"
                break
            time.sleep(1)
        return {
            "ok": False,
            "elapsed_seconds": round(time.monotonic() - started_at, 2),
            "package_dir": str(package_dir),
            "runtime_root": str(runtime_root),
            "verified_snapshot_seeded": verified_snapshot_seeded,
            "paths": checks,
            "web": web,
            "last_error": last_error,
            "stdout_tail": stdout_path.read_text(encoding="utf-8", errors="replace")[-2000:] if stdout_path.exists() else "",
        }
    finally:
        _terminate_process_tree(proc)


def main() -> int:
    reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure_stdout):
        reconfigure_stdout(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="验证打包产物空仓首启是否在限定时间内可用。")
    default_releases = Path(__file__).resolve().parents[3] / ".artifacts" / "hextech" / "releases"
    parser.add_argument("--package-dir", type=Path, help="已打包便携目录；默认使用 .artifacts/hextech/releases 下最新目录。")
    parser.add_argument("--dist-dir", type=Path, default=default_releases, help="便携包搜索根目录；默认是 .artifacts/hextech/releases。")
    parser.add_argument("--smoke-root", type=Path, default=Path(__file__).resolve().parents[2] / ".tmp_package_smoke", help="烟测复制副本根目录；默认是 run/.tmp_package_smoke。")
    parser.add_argument("--timeout", type=int, default=60, help="启动可用性等待秒数；默认 60。")
    parser.add_argument("--keep", action="store_true", help="保留复制出的烟测目录，便于排查。")
    args = parser.parse_args()

    source = args.package_dir or _latest_package(args.dist_dir)
    target = _copy_clean_package(source.resolve(), args.smoke_root.resolve())
    result = run_smoke(target, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["ok"] and not args.keep:
        if not _cleanup_smoke_root(target.parent):
            raise SmokeFailure(f"烟测进程退出后仍无法清理隔离目录：{target.parent}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
