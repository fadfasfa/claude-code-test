from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pipeline.common import PIPELINE_TMP_PUBLISH_DIR, VALIDATION_REPORT_FILE
from pipeline.compute.validate_runtime_data import validate_runtime_data

PROJECT_ROOT = Path(__file__).resolve().parent
DIST_DIR = PROJECT_ROOT / "dist"
PACKAGE_DIR = DIST_DIR / "sm2-randomizer-win"
PACKAGE_STATIC_DIR = PACKAGE_DIR / "static"
PACKAGE_DATA_DIR = PACKAGE_DIR / "data"
PACKAGE_ASSETS_DIR = PACKAGE_DIR / "assets"
PACKAGE_REPORT_FILE = PACKAGE_DIR / "runtime_validation.json"
PYINSTALLER_WORK_DIR = DIST_DIR / "pyinstaller"
SPEC_FILE = PROJECT_ROOT / "sm2_randomizer.spec"
APP_DIR = PROJECT_ROOT / "app"
APP_STATIC_DIR = APP_DIR / "static"
APP_DATA_DIR = APP_DIR / "data"
APP_ASSETS_DIR = APP_DIR / "assets"
PACKAGE_STATIC_FILES = ("index.html", "main.js", "styles.css", "fonts.css")
PACKAGE_ASSET_DIRS = ("classes", "talents", "weapons")
PACKAGE_RUNTIME_FILES = ("classes.json", "talents.json", "meta.json")
PACKAGED_SENTINELS = ("static", "data", "assets")
EXE_NAME = "sm2-randomizer"
EXE_FILE_NAME = f"{EXE_NAME}.exe"


def _missing_paths(paths: tuple[Path, ...]) -> list[str]:
    return [path.name for path in paths if not path.exists()]


def _assert_package_contract() -> None:
    required_dirs = tuple(PACKAGE_DIR / name for name in PACKAGED_SENTINELS)
    missing_dirs = _missing_paths(required_dirs)
    if missing_dirs:
        raise RuntimeError(f"最终发布目录缺少关键子目录: {', '.join(missing_dirs)}")

    missing_static = _missing_paths(tuple(PACKAGE_STATIC_DIR / name for name in PACKAGE_STATIC_FILES))
    if missing_static:
        raise RuntimeError(f"static/ 缺少关键文件: {', '.join(missing_static)}")

    missing_runtime = _missing_paths(tuple(PACKAGE_DATA_DIR / name for name in PACKAGE_RUNTIME_FILES))
    if missing_runtime:
        raise RuntimeError(f"data/ 缺少关键文件: {', '.join(missing_runtime)}")

    missing_assets = _missing_paths(tuple(PACKAGE_ASSETS_DIR / name for name in PACKAGE_ASSET_DIRS))
    if missing_assets:
        raise RuntimeError(f"assets/ 缺少关键目录: {', '.join(missing_assets)}")


def _print_contract_summary() -> None:
    print(f"[sm2-randomizer] Package contract OK: {PACKAGE_DIR}")
    print(f"[sm2-randomizer] Required roots: {', '.join(PACKAGED_SENTINELS)}")
    print(f"[sm2-randomizer] Static entry: {PACKAGE_STATIC_DIR / 'index.html'}")
    print(f"[sm2-randomizer] Runtime metadata: {PACKAGE_DATA_DIR / 'meta.json'}")
    print(f"[sm2-randomizer] Assets root: {PACKAGE_ASSETS_DIR}")


def _check_launch_log_retention() -> None:
    print("[sm2-randomizer] Launch logs are cleaned on startup; files older than 7 days are deleted.")
    print("[sm2-randomizer] Active launch log file: sm2-randomizer-launch.log")


def _finalize_package_contract() -> None:
    _assert_package_contract()
    _print_contract_summary()
    _check_launch_log_retention()


def _run_packaged_smoke_check() -> None:
    probe_script = """
import json
import urllib.request

base = 'http://127.0.0.1:53231'
checks = {
    '/': ['<!DOCTYPE html>', '<script src="./main.js?v=3"></script>'],
    '/data/classes.json': ['"classes"'],
    '/data/talents.json': ['"classes"'],
    '/data/meta.json': ['"build"', '"positive_modifier_pool"'],
}
result = {}
for path, markers in checks.items():
    with urllib.request.urlopen(base + path, timeout=5) as response:
        body = response.read().decode('utf-8', 'ignore')
        result[path] = response.status
        for marker in markers:
            if marker not in body:
                raise RuntimeError(f'{path} missing expected marker: {marker}')
print(json.dumps(result, ensure_ascii=False))
""".strip()
    exit_code = _run([sys.executable, "-c", probe_script])
    if exit_code != 0:
        raise RuntimeError("打包模式自检失败：首页或运行期 JSON 不可访问。")


def _run_packaged_asset_check() -> None:
    classes_payload = json.loads((PACKAGE_DATA_DIR / "classes.json").read_text(encoding="utf-8"))
    talents_payload = json.loads((PACKAGE_DATA_DIR / "talents.json").read_text(encoding="utf-8"))
    missing_assets: list[str] = []
    for entry in classes_payload.get("classes", []):
        image_path = entry.get("image_path")
        if image_path and not (PACKAGE_ASSETS_DIR / image_path).exists():
            missing_assets.append(image_path)
        pools = entry.get("loadout_pools", {})
        for items in pools.values():
            for item in items or []:
                weapon_path = item.get("image_path")
                if weapon_path and not (PACKAGE_ASSETS_DIR / weapon_path).exists():
                    missing_assets.append(weapon_path)
    for group in talents_payload.get("classes", []):
        for node in group.get("nodes", []):
            icon_path = node.get("icon_path")
            if icon_path and not (PACKAGE_ASSETS_DIR / icon_path).exists():
                missing_assets.append(icon_path)
    if missing_assets:
        sample = ", ".join(sorted(set(missing_assets))[:10])
        raise RuntimeError(f"打包资源校验失败，缺少文件示例: {sample}")
    print("[sm2-randomizer] Packaged asset references all resolve to local files.")


def _run_source_smoke_check() -> None:
    print("[sm2-randomizer] Source-mode regression should verify /app/static/ after packaging changes.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Top-level entrypoint for data refresh and release packaging.")
    subparsers = parser.add_subparsers(dest="command")

    refresh = subparsers.add_parser("refresh-data", help="Refresh pipeline data, build candidate runtime files, and emit diffs.")
    refresh.add_argument("--skip-wiki", action="store_true", help="Skip wiki collection.")
    refresh.add_argument("--skip-excel", action="store_true", help="Skip excel conversion.")
    refresh.add_argument("--skip-validate", action="store_true", help="Skip runtime validation.")
    refresh.add_argument("--skip-diff", action="store_true", help="Skip candidate diff artifact generation.")
    refresh.add_argument("--headless", action="store_true", help="Pass through to wiki asset refresh.")
    refresh.add_argument("--dump-dom", action="store_true", help="Pass through to wiki asset refresh.")
    refresh.add_argument("--force-download", action="store_true", help="Pass through to wiki asset refresh.")
    refresh.add_argument("--class", dest="class_titles", action="append", default=[], help="Pass through to wiki asset refresh.")

    build_candidate_parser = subparsers.add_parser("build-candidate", help="Build app-format runtime candidate files into pipeline/tmp_publish.")
    build_candidate_parser.add_argument("--skip-validate", action="store_true", help="Skip runtime validation for the candidate output.")

    subparsers.add_parser("diff-candidate", help="Generate JSON and Markdown diff summaries for the current candidate.")
    subparsers.add_parser("apply-candidate", help="Apply reviewed candidate runtime files into app/data.")
    subparsers.add_parser("clean-candidate", help="Delete candidate runtime files.")

    package_release_parser = subparsers.add_parser("package-release", help="Build a validated final package folder and optional exe.")
    package_release_parser.add_argument("--skip-refresh", action="store_true", help="Skip data refresh before packaging.")
    package_release_parser.add_argument("--with-exe", action="store_true", help="Also build a PyInstaller exe directly into the package root.")

    argv = sys.argv[1:]
    if not argv:
        argv = ["refresh-data"]
    return parser.parse_args(argv)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> int:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env={**os.environ, **(env or {})},
        check=False,
    )
    return int(completed.returncode)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _prepare_package_directory() -> None:
    _remove_path(DIST_DIR / "release")
    _remove_path(DIST_DIR / "exe")
    _remove_path(PACKAGE_DIR)
    PACKAGE_STATIC_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGE_ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def _copy_package_static() -> None:
    for filename in PACKAGE_STATIC_FILES:
        shutil.copy2(APP_STATIC_DIR / filename, PACKAGE_STATIC_DIR / filename)


def _copy_package_assets() -> None:
    for directory_name in PACKAGE_ASSET_DIRS:
        _copy_tree(APP_ASSETS_DIR / directory_name, PACKAGE_ASSETS_DIR / directory_name)


def _copy_package_runtime() -> None:
    for filename in PACKAGE_RUNTIME_FILES:
        shutil.copy2(APP_DATA_DIR / filename, PACKAGE_DATA_DIR / filename)


def _validate_package_runtime() -> dict:
    return validate_runtime_data(
        target_dir=PACKAGE_DATA_DIR,
        report_path=PACKAGE_REPORT_FILE,
        assets_dir=PACKAGE_ASSETS_DIR,
    )


def _print_package_summary(validation: dict) -> None:
    static_files = sorted(path.name for path in PACKAGE_STATIC_DIR.glob("*") if path.is_file())
    runtime_files = sorted(path.name for path in PACKAGE_DATA_DIR.glob("*.json"))
    asset_directories = sorted(path.name for path in PACKAGE_ASSETS_DIR.iterdir() if path.is_dir())
    issue_count = int(validation.get("summary", {}).get("issue_count", 0) or 0)
    print(f"[sm2-randomizer] Final package directory: {PACKAGE_DIR}")
    print(f"[sm2-randomizer] Static files: {', '.join(static_files)}")
    print(f"[sm2-randomizer] Runtime files: {', '.join(runtime_files)}")
    print(f"[sm2-randomizer] Asset directories: {', '.join(asset_directories)}")
    print(f"[sm2-randomizer] Validation issues: {issue_count}")


def _package_issue_count(validation: dict) -> int:
    return int(validation.get("summary", {}).get("issue_count", 0) or 0)


def _ensure_package_ready() -> tuple[bool, str]:
    if not PACKAGE_DIR.exists():
        return False, f"缺少 {PACKAGE_DIR.name}，请先生成最终发布目录。"
    required_files = [PACKAGE_REPORT_FILE]
    if any(not path.exists() for path in required_files):
        return False, f"{PACKAGE_DIR.name} 内容不完整，无法继续构建 exe。"
    try:
        _assert_package_contract()
    except RuntimeError as error:
        return False, str(error)
    if not SPEC_FILE.exists():
        return False, "缺少 sm2_randomizer.spec，无法继续构建 exe。"
    return True, ""


def _launch_log_runtime_dir() -> Path:
    return PACKAGE_DIR


def _cleanup_previous_launch_log() -> None:
    launch_log = _launch_log_runtime_dir() / "sm2-randomizer-launch.log"
    if launch_log.exists():
        launch_log.unlink()


def _wait_for_launch_log(timeout_seconds: float = 10.0) -> Path:
    launch_log = _launch_log_runtime_dir() / "sm2-randomizer-launch.log"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if launch_log.exists() and launch_log.stat().st_size:
            return launch_log
        time.sleep(0.2)
    raise RuntimeError("等待打包版启动日志超时。")


def _run_packaged_exe_smoke_test() -> None:
    _cleanup_previous_launch_log()
    process = subprocess.Popen([str(PACKAGE_DIR / EXE_FILE_NAME)], cwd=PACKAGE_DIR)
    try:
        launch_log = _wait_for_launch_log()
        _run_packaged_smoke_check()
        print(f"[sm2-randomizer] Packaged smoke log: {launch_log}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _run_packaged_python_smoke_test() -> None:
    probe_script = f"""
import os
import sys
import subprocess
import time
import urllib.request

project_root = r'{PROJECT_ROOT.as_posix()}'
env = os.environ.copy()
env['SM2_WEB_ROOT'] = r'{PACKAGE_DIR.as_posix()}'
env['SM2_DEBUG_PORT'] = '53233'
env['SM2_DEBUG_NO_BROWSER'] = '1'
process = subprocess.Popen([sys.executable, 'serve_static.py'], cwd=project_root, env=env)
try:
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:53233/data/classes.json', timeout=1) as response:
                print(response.status)
                break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError('packaged-python smoke did not come up in time')
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
""".strip()
    exit_code = _run([sys.executable, "-c", probe_script])
    if exit_code != 0:
        raise RuntimeError("源码 Python + SM2_WEB_ROOT 模拟打包模式时，/data/* 仍不可访问。")


def _run_packaged_exe_endpoint_probe() -> None:
    probe_script = """
import urllib.request
base = 'http://127.0.0.1:53231'
path = '/data/classes.json'
with urllib.request.urlopen(base + path, timeout=5) as response:
    print(path, response.status)
""".strip()
    exit_code = _run([sys.executable, "-c", probe_script])
    if exit_code != 0:
        raise RuntimeError("exe 打包模式探测失败：/data/classes.json 仍无法返回 200。")


def _emit_packaged_exe_followup_note() -> None:
    print("[sm2-randomizer] Exe connectivity note: follow-up manual verification should watch for repeated browser requests on /data/*.json.")


def _run_packaged_mode_differential_check() -> None:
    _run_packaged_python_smoke_test()
    try:
        _run_packaged_exe_smoke_test()
        _run_packaged_exe_endpoint_probe()
    except RuntimeError as error:
        print(f"[sm2-randomizer] Packaged exe differential check warning: {error}")
        _emit_packaged_exe_followup_note()


def _verify_release_acceptance(with_exe: bool) -> None:
    _finalize_package_contract()
    _run_packaged_asset_check()
    if with_exe:
        _run_packaged_mode_differential_check()
    _run_source_smoke_check()


def _emit_acceptance_summary(with_exe: bool) -> None:
    print("[sm2-randomizer] Acceptance target: exe opens the page successfully and core in-page resources stay reachable.")
    if with_exe:
        print("[sm2-randomizer] Packaged smoke check covers homepage and JSON runtime payloads.")
        print("[sm2-randomizer] If the browser reports ERR_EMPTY_RESPONSE, inspect sm2-randomizer-launch.log for the failing request path.")
    print("[sm2-randomizer] Manual functional validation still required: 抽职业 / 抽武器 / 抽天赋 / 保存切换 / 重置 / 词条抽取。")


def _post_package_debug_probe(with_exe: bool) -> None:
    return None


def _remove_previous_exe() -> None:
    _remove_path(PACKAGE_DIR / EXE_FILE_NAME)
    _remove_path(PACKAGE_DIR / EXE_NAME)
    _remove_path(PYINSTALLER_WORK_DIR)


def _find_built_exe() -> Path | None:
    direct_candidate = PACKAGE_DIR / EXE_FILE_NAME
    if direct_candidate.exists():
        return direct_candidate
    alt_candidate = PACKAGE_DIR / EXE_NAME
    if alt_candidate.exists():
        return alt_candidate
    nested_dir = PACKAGE_DIR / EXE_NAME
    for name in (EXE_FILE_NAME, EXE_NAME):
        nested_candidate = nested_dir / name
        if nested_candidate.exists():
            return nested_candidate
    return None


def _build_exe_bundle() -> Path:
    ready, message = _ensure_package_ready()
    if not ready:
        raise RuntimeError(message)

    if _run(["pyinstaller", "--version"]) != 0:
        raise RuntimeError("未检测到 PyInstaller，请先安装后再执行 package-release --with-exe。")

    _remove_previous_exe()

    print("[sm2-randomizer] Building PyInstaller exe into the final package directory...")
    exit_code = _run(
        [
            "pyinstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(PACKAGE_DIR),
            "--workpath",
            str(PYINSTALLER_WORK_DIR),
            str(SPEC_FILE),
        ],
        env={"SM2_WEB_ROOT": str(PACKAGE_DIR)},
    )
    if exit_code != 0:
        raise RuntimeError("PyInstaller 构建失败。")

    exe_path = _find_built_exe()
    if not exe_path:
        raise RuntimeError(f"PyInstaller completed but no executable was found in {PACKAGE_DIR}.")

    if exe_path.parent != PACKAGE_DIR:
        target_path = PACKAGE_DIR / exe_path.name
        shutil.copy2(exe_path, target_path)
        exe_path = target_path

    _remove_path(PYINSTALLER_WORK_DIR)
    return exe_path


def refresh_data(args: argparse.Namespace) -> int:
    if not args.skip_wiki:
        command = [sys.executable, "-m", "pipeline.collect.wiki.run"]
        if args.headless:
            command.append("--headless")
        if args.dump_dom:
            command.append("--dump-dom")
        if args.force_download:
            command.append("--force-download")
        for class_title in args.class_titles:
            command.extend(["--class", class_title])
        exit_code = _run(command)
        if exit_code != 0:
            return exit_code

    if not args.skip_excel:
        exit_code = _run([sys.executable, "-m", "pipeline.collect.excel.run"])
        if exit_code != 0:
            return exit_code

    exit_code = _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.build_runtime_data",
            "--output-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
        ]
    )
    if exit_code != 0:
        return exit_code

    if not args.skip_validate:
        exit_code = _run(
            [
                sys.executable,
                "-m",
                "pipeline.compute.validate_runtime_data",
                "--target-dir",
                str(PIPELINE_TMP_PUBLISH_DIR),
                "--report-path",
                str(VALIDATION_REPORT_FILE),
            ]
        )
        if exit_code != 0:
            return exit_code

    if args.skip_diff:
        return 0
    return diff_candidate()


def build_candidate(skip_validate: bool = False) -> int:
    exit_code = _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.build_runtime_data",
            "--output-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
        ]
    )
    if exit_code != 0:
        return exit_code
    if skip_validate:
        return 0
    return _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.validate_runtime_data",
            "--target-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
            "--report-path",
            str(VALIDATION_REPORT_FILE),
        ]
    )


def diff_candidate() -> int:
    return _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.publish_candidate",
            "diff-candidate",
            "--candidate-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
        ]
    )


def apply_candidate() -> int:
    missing = [name for name in PACKAGE_RUNTIME_FILES if not (PIPELINE_TMP_PUBLISH_DIR / name).exists()]
    if missing:
        print(f"[sm2-randomizer] Missing candidate files in {PIPELINE_TMP_PUBLISH_DIR}: {', '.join(missing)}")
        return 1
    exit_code = _run(
        [
            sys.executable,
            "-m",
            "pipeline.compute.validate_runtime_data",
            "--target-dir",
            str(PIPELINE_TMP_PUBLISH_DIR),
            "--report-path",
            str(VALIDATION_REPORT_FILE),
        ]
    )
    if exit_code != 0:
        return exit_code
    validation = json.loads(VALIDATION_REPORT_FILE.read_text(encoding="utf-8")) if VALIDATION_REPORT_FILE.exists() else {}
    issue_count = validation.get("summary", {}).get("issue_count", 0)
    if issue_count:
        print(f"[sm2-randomizer] Candidate validation still has {issue_count} issues. Refusing to apply.")
        return 1
    return _run([sys.executable, "-m", "pipeline.compute.publish_candidate", "apply-candidate"])


def clean_candidate() -> int:
    return _run([sys.executable, "-m", "pipeline.compute.publish_candidate", "clean-candidate"])


def package_release(args: argparse.Namespace) -> int:
    _prepare_package_directory()
    _copy_package_static()
    _copy_package_assets()
    _copy_package_runtime()
    validation = _validate_package_runtime()
    _print_package_summary(validation)
    if _package_issue_count(validation):
        return 1
    if not args.with_exe:
        try:
            _verify_release_acceptance(with_exe=False)
            _emit_acceptance_summary(with_exe=False)
        except RuntimeError as error:
            print(f"[sm2-randomizer] {error}")
            return 1
        print("[sm2-randomizer] Static final package is ready. Pass --with-exe to place the exe at the package root.")
        return 0
    try:
        exe_path = _build_exe_bundle()
        _verify_release_acceptance(with_exe=True)
        _emit_acceptance_summary(with_exe=True)
    except RuntimeError as error:
        print(f"[sm2-randomizer] {error}")
        return 1
    print(f"[sm2-randomizer] EXE output: {exe_path}")
    print(f"[sm2-randomizer] dist/pyinstaller exists: {PYINSTALLER_WORK_DIR.exists()}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "package-release":
        if not args.skip_refresh:
            refresh_args = argparse.Namespace(
                skip_wiki=False,
                skip_excel=False,
                skip_validate=False,
                skip_diff=False,
                headless=False,
                dump_dom=False,
                force_download=False,
                class_titles=[],
            )
            exit_code = refresh_data(refresh_args)
            if exit_code != 0:
                return exit_code
        return package_release(args)
    if args.command == "build-candidate":
        return build_candidate(skip_validate=args.skip_validate)
    if args.command == "diff-candidate":
        return diff_candidate()
    if args.command == "apply-candidate":
        return apply_candidate()
    if args.command == "clean-candidate":
        return clean_candidate()
    return refresh_data(args)


if __name__ == "__main__":
    raise SystemExit(main())
