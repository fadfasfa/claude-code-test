"""发布包构建工具。

打包过程中不在 `run/` 下创建长期 `build/`、`dist/` 或 `_bundle_runtime/`
资源副本；PyInstaller 的 work/dist/spec 全部落到系统临时目录，最终只把
便携目录和 zip 移入仓库根 `.artifacts/hextech/releases/`。

调用方: build; 关键依赖: bundle_manifest、cleanup_runtime、package_rules。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tools.bundle_manifest import build_bundle_manifest, validate_hextech_seed_health  # noqa: E402
from tools.cleanup_runtime import cleanup_python_caches  # noqa: E402
from tools.package_rules import iter_package_data_entries  # noqa: E402


REPO_DIR = BASE_DIR.parent
ARTIFACTS_DIR = REPO_DIR / ".artifacts" / "hextech"
RELEASES_DIR = ARTIFACTS_DIR / "releases"
STAGING_RELEASES_DIR = ARTIFACTS_DIR / "staging"
APP_EXE_NAME = "Hextech伴生终端.exe"
APP_BUILD_NAME = "Hextech伴生终端"
RELEASE_PREFIX = "HextechCompanion"
LAUNCHER_NAME = "启动 Hextech.bat"
FIRST_RUN_GUIDE_NAME = "README_首次使用.txt"
EXCLUDED_MODULES = [
    "tkinter.test",
    "unittest",
    "pydoc",
    "scipy",
    "matplotlib",
    "botocore",
    "boto3",
    "s3transfer",
    "jmespath",
]
PYINSTALLER_HIDDEN_IMPORTS = [
    "pandas",
    "numpy",
    "requests",
    "PIL",
    "PIL.ImageTk",
    "tkinter",
    "_tkinter",
    "tkinter.ttk",
    "win32gui",
    "win32con",
    "psutil",
    "fastapi",
    "uvicorn",
    "filelock",
    "bs4",
    "scrapling.fetchers",
    "cloakbrowser",
    "hextech",
    "hextech.display.desktop.app",
    "hextech.display.web.app",
    "hextech.overlay.data_source",
    "hextech.overlay.host",
    "hextech.overlay.lifecycle",
    "hextech.overlay.renderer",
    "hextech.overlay.vision.sidecar",
    "hextech.core.settings",
    "hextech.data_service",
    "hextech.data_snapshot",
]
PYINSTALLER_COLLECT_SUBMODULES = [
    "tkinter",
    "fastapi",
    "starlette",
    "uvicorn",
    "scrapling",
    "cloakbrowser",
    "hextech",
]
PYINSTALLER_COLLECT_DATA = [
    "scrapling",
    "browserforge",
    "apify_fingerprint_datapoints",
]
LEGACY_GENERATED_OUTPUTS = (
    BASE_DIR / "build",
    BASE_DIR / "dist",
    BASE_DIR / "version_info.txt",
    BASE_DIR / "Hextech伴生终端.spec",
)
REQUIRED_PACKAGED_SCRAPING_DATA = (
    "browser-helper-file.json",
    "fingerprint-network-definition.zip",
    "header-network-definition.zip",
    "headers-order.json",
    "input-network-definition.zip",
)


def print_step(msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}\n")


def print_check(msg: str) -> None:
    print(f"  [成功] {msg}")


def print_error(msg: str) -> None:
    print(f"  [失败] {msg}")


def print_warn(msg: str) -> None:
    print(f"  [警告] {msg}")


def cleanup_legacy_outputs() -> list[Path]:
    """删除旧打包链路遗留的仓库内生成物。"""

    removed: list[Path] = []
    for target in LEGACY_GENERATED_OUTPUTS:
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(target)
    return removed


def cleanup() -> None:
    """清理旧构建输出和 Python 缓存，保证打包环境干净。"""

    print_step("清理旧构建文件")
    for target in cleanup_legacy_outputs():
        print_check(f"已删除旧生成物：{target}")
    removed_dirs, removed_files = cleanup_python_caches()
    print_check(f"已清理 Python 缓存目录 {removed_dirs} 个，缓存文件 {removed_files} 个")


def generate_version_info(build_root: Path) -> Path:
    """生成供 PyInstaller 注入的 Windows 版本信息文件。"""

    print_step("生成版本信息")
    version_file = build_root / "version_info.txt"
    version_content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({datetime.now().year}, 4, 7, 0),
    prodvers=({datetime.now().year}, 4, 7, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Hextech Nexus'),
          StringStruct('FileDescription', 'Hextech 伴生系统 - 英雄联盟海克斯数据分析工具'),
          StringStruct('FileVersion', '{datetime.now().strftime("%Y.%m.%d.%H")}'),
          StringStruct('InternalName', 'HextechTerminal'),
          StringStruct('LegalCopyright', 'Copyright © Hextech Nexus'),
          StringStruct('OriginalFilename', 'Hextech伴生终端.exe'),
          StringStruct('ProductName', 'Hextech Companion'),
          StringStruct('ProductVersion', '{datetime.now().strftime("%Y.%m.%d")}'),
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
"""
    version_file.write_text(version_content, encoding="utf-8")
    print_check(f"版本信息已生成: {version_file}")
    return version_file


def write_generated_manifest(build_root: Path, *, verified_snapshot_root: Path | None = None) -> Path:
    """把本次 bundle manifest 写入临时 staging 目录。"""

    print_step("生成资源清单")
    staging_dir = build_root / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = staging_dir / "bundle_manifest.json"
    manifest = build_bundle_manifest(BASE_DIR, verified_snapshot_root=verified_snapshot_root)

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print_check("静态页面、稳定 data、快照和 assets 已按源路径加入打包规则")
    print_warn("不会在 run/build/_bundle_runtime 创建资源副本")
    return manifest_path


def _python_root_from_pyinstaller(pyinstaller_path: Path) -> Path | None:
    """从 PyInstaller 可执行文件位置推断其所属 Python 根目录。"""

    parent = pyinstaller_path.parent
    if parent.name.lower() in {"scripts", "bin"}:
        return parent.parent
    return None


def resolve_pyinstaller_command() -> tuple[list[str], Path]:
    """返回 PyInstaller 命令和与它匹配的 Python 根目录。"""

    module_probe = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if module_probe.returncode == 0:
        return [sys.executable, "-m", "PyInstaller"], Path(sys.base_prefix)

    pyinstaller = shutil.which("pyinstaller")
    if not pyinstaller:
        raise RuntimeError("未找到 PyInstaller；请安装到当前 Python，或确保 pyinstaller.exe 在 PATH 中。")
    pyinstaller_path = Path(pyinstaller).resolve()
    python_root = _python_root_from_pyinstaller(pyinstaller_path) or Path(sys.base_prefix)
    if python_root != Path(sys.base_prefix):
        print_warn(f"使用 PATH 中的 PyInstaller：{pyinstaller_path}")
        print_warn(f"Tcl/Tk 数据将匹配 PyInstaller 所属 Python：{python_root}")
    return [str(pyinstaller_path)], python_root


def resolve_tcl_runtime_dirs(python_root: Path) -> tuple[Path, Path, Path | None]:
    """返回当前解释器的 Tcl/Tk 数据目录，供 PyInstaller runtime hook 使用。"""

    candidates = [
        python_root / "tcl",
        Path(sys.base_prefix) / "tcl",
        Path(sys.prefix) / "tcl",
    ]
    for candidate in candidates:
        tcl_dir = candidate / "tcl8.6"
        tk_dir = candidate / "tk8.6"
        module_dir = candidate / "tcl8"
        if tcl_dir.is_dir() and tk_dir.is_dir():
            return tcl_dir, tk_dir, module_dir if module_dir.is_dir() else None
    searched = "、".join(str(path) for path in candidates)
    raise RuntimeError(f"当前 Python 缺少 Tcl/Tk 数据目录，无法打包 Tk UI：{searched}")


def resolve_tkinter_package_dir(python_root: Path) -> Path:
    """返回 stdlib tkinter package；部分 PyInstaller 环境不会自动放入 PYZ。"""

    candidates = [
        python_root / "Lib" / "tkinter",
        Path(sys.base_prefix) / "Lib" / "tkinter",
        Path(sys.prefix) / "Lib" / "tkinter",
    ]
    for candidate in candidates:
        if (candidate / "__init__.py").is_file():
            return candidate
    searched = "、".join(str(path) for path in candidates)
    raise RuntimeError(f"当前 Python 缺少 tkinter 标准库目录，无法打包 Tk UI：{searched}")


def stage_tkinter_package_dir(source: Path, build_root: Path) -> Path:
    """复制最小 Tkinter data 包，避免 stdlib 测试和缓存文件进入发布包。"""

    target = build_root / "staging" / "tkinter"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "test", "*.pyc", "*.pyo"),
    )
    return target


def parse_build_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析构建参数；无参数时保持原有打包入口，但默认不访问网络。"""

    parser = argparse.ArgumentParser(description="构建 Hextech 伴生系统便携包")
    parser.add_argument(
        "--refresh-data",
        action="store_true",
        help="构建前显式刷新远端数据；degraded/failed 会终止构建",
    )
    parser.add_argument(
        "--verified-snapshot-root",
        type=Path,
        help="注入已通过真实链路验收的 snapshots 根；仅用于离线构建同代验收包。",
    )
    args = parser.parse_args(argv)
    if args.refresh_data and args.verified_snapshot_root is not None:
        parser.error("--refresh-data 与 --verified-snapshot-root 不能同时使用")
    if args.verified_snapshot_root is not None:
        args.verified_snapshot_root = args.verified_snapshot_root.resolve()
        if not args.verified_snapshot_root.is_dir():
            parser.error("--verified-snapshot-root 必须是存在的目录")
    return args


def prepare_runtime_data_for_package(*, refresh_data: bool) -> None:
    """可选刷新数据，并校验实际进入发布包的 seed 健康信息。"""

    if refresh_data:
        print_step("显式刷新运行时数据")
        from hextech.core.refresh import refresh_backend_data

        result = refresh_backend_data(force=True)
        state = str(getattr(result, "state", "") or "")
        if state != "ready":
            reason_code = str(getattr(result, "reason_code", "") or "unknown")
            raise RuntimeError(f"构建前数据刷新未达到 ready：state={state or 'unknown'} reason={reason_code}")
        print_check("运行时数据刷新完成：state=ready")
    else:
        print_step("离线校验打包 seed")
        print_check("未指定 --refresh-data，跳过远端刷新")

    seed_health = validate_hextech_seed_health(BASE_DIR)
    print_check(
        "Hextech seed：file={filename} mtime={mtime} rows={rows} heroes={unique_heroes} source=data/seed/startup/hextech".format(
            **seed_health
        )
    )


def refresh_runtime_data_before_package() -> None:
    """兼容旧调用方：显式执行构建前刷新。"""

    prepare_runtime_data_for_package(refresh_data=True)


def _add_data_arg(source: Path, target: str) -> str:
    return f"{source};{target}"


def build_exe(
    version_file: Path,
    manifest_path: Path,
    build_root: Path,
    *,
    verified_snapshot_root: Path | None = None,
) -> Path:
    """执行 PyInstaller 主构建流程，并返回临时原始产物目录。"""

    print_step("构建可执行文件")
    work_path = build_root / "pyinstaller-work"
    dist_path = build_root / "pyinstaller-dist"
    spec_path = work_path
    pyinstaller_cmd, pyinstaller_python_root = resolve_pyinstaller_command()
    tcl_runtime_dir, tk_runtime_dir, tcl_module_dir = resolve_tcl_runtime_dirs(pyinstaller_python_root)
    tkinter_package_dir = resolve_tkinter_package_dir(pyinstaller_python_root)
    staged_tkinter_package_dir = stage_tkinter_package_dir(tkinter_package_dir, build_root)
    cmd = [
        *pyinstaller_cmd,
        "--clean",
        "--noconfirm",
        "--name",
        APP_BUILD_NAME,
        "--onedir",
        "--console",
        "--icon",
        "NONE",
        "--version-file",
        str(version_file),
        "--workpath",
        str(work_path),
        "--distpath",
        str(dist_path),
        "--specpath",
        str(spec_path),
    ]
    for entry in iter_package_data_entries(
        BASE_DIR,
        manifest_path,
        verified_snapshot_root=verified_snapshot_root,
    ):
        cmd.extend(["--add-data", _add_data_arg(entry.source, entry.target)])
    for source, target in (
        (tcl_runtime_dir, "_tcl_data"),
        (tk_runtime_dir, "_tk_data"),
        (staged_tkinter_package_dir, "tkinter"),
    ):
        cmd.extend(["--add-data", _add_data_arg(source, target)])
    if tcl_module_dir is not None:
        cmd.extend(["--add-data", _add_data_arg(tcl_module_dir, "tcl8")])
    for module_name in PYINSTALLER_HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", module_name])
    for module_name in PYINSTALLER_COLLECT_SUBMODULES:
        cmd.extend(["--collect-submodules", module_name])
    for module_name in PYINSTALLER_COLLECT_DATA:
        cmd.extend(["--collect-data", module_name])
    for module_name in EXCLUDED_MODULES:
        cmd.extend(["--exclude-module", module_name])
    cmd.append("hextech_ui.py")

    try:
        subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        print_error(f"构建失败：\n{exc.stderr}")
        sys.exit(1)

    print_check("构建成功")
    return dist_path / APP_BUILD_NAME


def write_portable_launcher(final_dir: Path) -> Path:
    """生成 ASCII/CRLF 启动脚本，避免中文 EXE 名和代码页破坏命令。"""

    launcher_path = final_dir / LAUNCHER_NAME
    launcher_content = (
        "@echo off\r\n"
        "setlocal EnableExtensions EnableDelayedExpansion\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set \"HEXTECH_EXE=\"\r\n"
        "set /a HEXTECH_EXE_COUNT=0\r\n"
        "for %%F in (*.exe) do (\r\n"
        "  set /a HEXTECH_EXE_COUNT+=1\r\n"
        "  set \"HEXTECH_EXE=%%~fF\"\r\n"
        ")\r\n"
        "if not !HEXTECH_EXE_COUNT!==1 exit /b 2\r\n"
        "if defined HEXTECH_LAUNCHER_WAIT (\r\n"
        "  \"!HEXTECH_EXE!\"\r\n"
        "  exit /b !errorlevel!\r\n"
        ")\r\n"
        "start \"\" \"!HEXTECH_EXE!\"\r\n"
    )
    launcher_path.write_bytes(launcher_content.encode("ascii"))
    return launcher_path


def write_first_run_guide(final_dir: Path) -> Path:
    """生成面向熟人分发的首次使用说明。"""

    guide_path = final_dir / FIRST_RUN_GUIDE_NAME
    guide_content = """Hextech 伴生系统 首次使用说明

1. 先把整个压缩包完整解压，不要只拿出单个 exe。
2. 解压后，直接双击“启动 Hextech.bat”。
3. 如果系统提示拦截：
   - 这是 Windows 对未签名应用的保护提示，不代表程序本身损坏。
   - 请在提示页里选择“更多信息”后再选择“仍要运行”（如果系统给出这个入口）。
4. 如果还是打不开：
   - 请把整个文件夹放到普通目录后再试，例如 D 盘或你自己新建的工作目录。
   - 不要放在只读、受限或同步中的目录里。

说明：
- 这是未签名的便携版，适合熟人或测试用户使用。
- 首次运行时，程序会自动补齐部分运行时数据。
"""
    guide_path.write_text(guide_content, encoding="utf-8")
    return guide_path


def validate_packaged_scraping_data(final_dir: Path) -> None:
    """阻止缺少 browserforge/apify 模型数据的包进入 smoke。"""

    data_dir = final_dir / "_internal" / "apify_fingerprint_datapoints" / "data"
    missing = [name for name in REQUIRED_PACKAGED_SCRAPING_DATA if not (data_dir / name).is_file()]
    if missing:
        raise RuntimeError("打包缺少 Scrapling 指纹数据：" + ", ".join(missing))


def create_portable_zip(final_dir: Path) -> Path:
    """把最终目录压缩为便于发送的便携包。"""

    zip_path = RELEASES_DIR / f"{final_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
        for path in final_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".log") or path.name.endswith(".lock"):
                continue
            zf.write(path, arcname=str(Path(final_dir.name) / path.relative_to(final_dir)))
    return zip_path


def _release_dir_name(build_time: datetime) -> str:
    return f"{RELEASE_PREFIX}-{build_time.strftime('%Y%m%d')}"


def run_packaged_smoke(package_dir: Path, timeout: int = 60) -> None:
    """运行便携包首启 smoke；失败时调用方不得替换正式 release。"""

    smoke_script = BASE_DIR / "tools" / "acceptance" / "smoke_packaged_startup.py"
    subprocess.run(
        [
            sys.executable,
            str(smoke_script),
            "--package-dir",
            str(package_dir),
            "--timeout",
            str(timeout),
        ],
        cwd=BASE_DIR,
        text=True,
        check=True,
    )


def _move_existing_release_as_backup(final_dir: Path, zip_path: Path, build_time: datetime) -> tuple[Path | None, Path | None]:
    backup_suffix = build_time.strftime("%H%M%S")
    backup_dir = None
    backup_zip = None
    if final_dir.exists():
        backup_dir = final_dir.with_name(f"{final_dir.name}.previous-{backup_suffix}")
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        shutil.move(str(final_dir), str(backup_dir))
    if zip_path.exists():
        backup_zip = zip_path.with_name(f"{zip_path.stem}.previous-{backup_suffix}{zip_path.suffix}")
        if backup_zip.exists():
            backup_zip.unlink()
        shutil.move(str(zip_path), str(backup_zip))
    return backup_dir, backup_zip


def _restore_release_backup(final_dir: Path, zip_path: Path, backup_dir: Path | None, backup_zip: Path | None) -> None:
    if backup_dir is not None and backup_dir.exists():
        if final_dir.exists():
            failed_dir = final_dir.with_name(f"{final_dir.name}.failed")
            if failed_dir.exists():
                shutil.rmtree(failed_dir)
            shutil.move(str(final_dir), str(failed_dir))
        shutil.move(str(backup_dir), str(final_dir))
    if backup_zip is not None and backup_zip.exists():
        if zip_path.exists():
            failed_zip = zip_path.with_name(f"{zip_path.stem}.failed{zip_path.suffix}")
            if failed_zip.exists():
                failed_zip.unlink()
            shutil.move(str(zip_path), str(failed_zip))
        shutil.move(str(backup_zip), str(zip_path))


def _discard_release_backup(backup_dir: Path | None, backup_zip: Path | None) -> None:
    if backup_dir is not None and backup_dir.exists():
        shutil.rmtree(backup_dir)
    if backup_zip is not None and backup_zip.exists():
        backup_zip.unlink()


def finalize_output(exe_dir: Path) -> tuple[Path, Path]:
    """整理最终输出目录；先 smoke staging，通过后才替换正式 release。"""

    print_step("最终优化")
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    build_time = datetime.now()
    final_dir = RELEASES_DIR / _release_dir_name(build_time)
    zip_path = RELEASES_DIR / f"{final_dir.name}.zip"
    staging_dir = STAGING_RELEASES_DIR / f"{final_dir.name}-{build_time.strftime('%H%M%S')}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    shutil.move(str(exe_dir), str(staging_dir))
    write_portable_launcher(staging_dir)
    write_first_run_guide(staging_dir)
    validate_packaged_scraping_data(staging_dir)
    print_check(f"staging 输出目录：{staging_dir}")
    run_packaged_smoke(staging_dir, timeout=60)

    backup_dir, backup_zip = _move_existing_release_as_backup(final_dir, zip_path, build_time)
    try:
        shutil.move(str(staging_dir), str(final_dir))
        zip_path = create_portable_zip(final_dir)
    except Exception:
        _restore_release_backup(final_dir, zip_path, backup_dir, backup_zip)
        raise
    else:
        _discard_release_backup(backup_dir, backup_zip)
    print_check(f"输出目录：{final_dir}")
    print_check(f"便携压缩包：{zip_path}")
    return final_dir, zip_path


def main(argv: list[str] | None = None) -> None:
    """打包工具主流程入口。"""

    args = parse_build_args(argv)
    print("\n" + "=" * 60)
    print("  Hextech 伴生系统打包程序")
    print(f"  构建时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    cleanup()
    prepare_runtime_data_for_package(refresh_data=args.refresh_data)
    with TemporaryDirectory(prefix="hextech-build-") as tmp_dir:
        build_root = Path(tmp_dir)
        manifest_path = write_generated_manifest(
            build_root,
            verified_snapshot_root=args.verified_snapshot_root,
        )
        version_file = generate_version_info(build_root)
        exe_dir = build_exe(
            version_file,
            manifest_path,
            build_root,
            verified_snapshot_root=args.verified_snapshot_root,
        )
        final_dir, zip_path = finalize_output(exe_dir)
    print_step("打包完成")
    print(f"  输出目录：{final_dir}")
    print(f"  主程序：{final_dir / APP_EXE_NAME}")
    print(f"  启动脚本：{final_dir / LAUNCHER_NAME}")
    print(f"  首次说明：{final_dir / FIRST_RUN_GUIDE_NAME}")
    print(f"  便携压缩包：{zip_path}")


if __name__ == "__main__":
    main()
