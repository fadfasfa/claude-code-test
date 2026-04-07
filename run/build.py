# Hextech 伴生系统打包脚本
# - 重构打包流程，添加数字签名支持
# - 优化 PyInstaller 配置避免系统拦截
# - 自动生成版本信息

import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime


BASE_DIR = Path(__file__).parent
DIST_DIR = BASE_DIR / "dist"
BUILD_DIR = BASE_DIR / "build"
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


def print_step(msg: str):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}\n")

# 兼容系统控制台编码
def print_check(msg: str):
    print(f"  [成功] {msg}")

def print_error(msg: str):
    print(f"  [失败] {msg}")

def print_warn(msg: str):
    print(f"  [警告] {msg}")


def _cleanup_python_caches() -> None:
    # 清理源码目录里的 Python 字节码缓存，避免工作区脏污，也避免误以为会被打包。
    removed_dirs = 0
    removed_files = 0

    for cache_dir in BASE_DIR.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
            removed_dirs += 1

    for pattern in ("*.pyc", "*.pyo"):
        for pyc_file in BASE_DIR.rglob(pattern):
            if pyc_file.is_file():
                try:
                    pyc_file.unlink()
                    removed_files += 1
                except OSError:
                    pass

    print_check(f"已清理 Python 缓存目录 {removed_dirs} 个，缓存文件 {removed_files} 个")


def _prepare_runtime_bundle() -> None:
    # 打包时只保留运行壳和静态页面，数据与缓存一律在首次启动后自动拉取。
    print_step("准备最小运行资源")

    bundle_static_dir = BUILD_DIR / "_bundle_static"
    if bundle_static_dir.exists():
        shutil.rmtree(bundle_static_dir)
    shutil.copytree(BASE_DIR / "static", bundle_static_dir)

    print_check("静态页面已复制到临时打包目录")
    print_warn("config 目录不再打包，运行后将自动生成并刷新最新数据")
    print_warn("assets 目录不再打包，缺失图标将在运行期自动缓存")


def cleanup():
    # 清理旧的构建产物
    print_step("清理旧构建文件")
    for d in [DIST_DIR, BUILD_DIR]:
        if d.exists():
            shutil.rmtree(d)
            print_check(f"已删除：{d}")
    _cleanup_python_caches()
    print_check("清理完成")


def generate_version_info():
    # 生成版本信息文件
    print_step("生成版本信息")

    version_file = BASE_DIR / "version_info.txt"
    version_content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({datetime.now().year}, 3, 24, 0),
    prodvers=({datetime.now().year}, 3, 24, 0),
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
          StringStruct('LegalCopyright', f'Copyright © 2023-{datetime.now().year} Hextech Nexus'),
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
    version_file.write_text(version_content, encoding='utf-8')
    print_check(f"版本信息已生成: {version_file}")
    return version_file


def build_exe(version_file: Path) -> Path:
    # 使用打包工具构建可执行文件。
    print_step("构建可执行文件")

    bundle_static_dir = BUILD_DIR / "_bundle_static"
    if not bundle_static_dir.exists():
        _prepare_runtime_bundle()

    # 优化后的打包命令
    cmd = [
        "pyinstaller",
        "--clean",
        "--noconfirm",
        "--name", "Hextech伴生终端",
        "--onedir",  # 使用文件夹模式（签名更容易）
        "--console",
        "--icon", "NONE",
        "--version-file", str(version_file),
        "--add-data", f"{bundle_static_dir};static",
        "--hidden-import", "pandas",
        "--hidden-import", "numpy",
        "--hidden-import", "requests",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "win32gui",
        "--hidden-import", "psutil",
        "--hidden-import", "fastapi",
        "--hidden-import", "uvicorn",
        "--collect-submodules", "uvicorn",
        "hextech_ui.py"
    ]

    for module_name in EXCLUDED_MODULES:
        cmd.extend(["--exclude-module", module_name])

    print(f"  执行命令：{' '.join(cmd)}")
    try:
        subprocess.run(
            cmd,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print_error(f"构建失败：\n{exc.stderr}")
        sys.exit(1)

    print_check("构建成功")
    return DIST_DIR / "Hextech伴生终端"


def sign_exe(exe_dir: Path):
    # 对可执行文件进行数字签名。
    print_step("数字签名")

    exe_path = exe_dir / "Hextech伴生终端.exe"

    if not exe_path.exists():
        print_error(f"找不到可执行文件：{exe_path}")
        return False

    # 检查签名工具是否可用
    try:
        result = subprocess.run(
            ["signtool", "sign", "/?"],
            capture_output=True,
            text=True,
            check=False,
        )
        signtool_available = result.returncode == 0
    except FileNotFoundError:
        signtool_available = False

    if not signtool_available:
        print_warn("未找到 signtool，跳过数字签名")
        print_warn("提示：请安装 Windows SDK 或使用证书签名工具")
        return False

    # 尝试使用时间戳服务器进行签名
    timestamp_urls = [
        "http://timestamp.digicert.com",
        "http://timestamp.sectigo.com",
        "http://timestamp.globalsign.com"
    ]

    for ts_url in timestamp_urls:
        cmd = [
            "signtool", "sign",
            "/fd", "SHA256",
            "/td", "SHA256",
            "/tr", ts_url,
            "/v",
            str(exe_path)
        ]

        # 如果有证书文件，添加签名参数
        cert_file = BASE_DIR / "certificate.pfx"
        if cert_file.exists():
            cmd.extend(["/f", str(cert_file), "/p", "YOUR_CERT_PASSWORD"])
        else:
            # 尝试使用系统证书存储
            cmd.extend(["/n", "Hextech Nexus"])

        print(f"  尝试签名：{ts_url}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if result.returncode == 0:
            print_check(f"签名成功（时间戳：{ts_url}）")

            # 验证签名
            verify_cmd = ["signtool", "verify", "/v", "/pa", str(exe_path)]
            verify_result = subprocess.run(
                verify_cmd,
                capture_output=True,
                text=True,
                check=False,
            )

            if verify_result.returncode == 0:
                print_check("签名验证通过")
                return True
            else:
                print_warn(f"签名验证失败：\n{verify_result.stdout}")

    print("  所有签名尝试均失败")
    return False


def create_readme(exe_dir: Path):
    # 创建使用说明文档
    print_step("生成使用文档")

    readme = exe_dir / "使用说明.txt"
    readme_content = f"""
Hextech 伴生系统
=====================

版本: {datetime.now().strftime("%Y.%m.%d")}
构建时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

（首次运行说明）
1. 如果系统提示"未知发布者"，请点击"详细信息" 到 "仍要运行"
2. 建议将本程序添加到 Windows Defender 白名单
3. 右键程序 到 属性 到 勾选"解除锁定"

（功能简介）
- 实时监控英雄联盟选人阶段
- 自动显示备战席英雄的海克斯数据
- 一键跳转英雄详情页面
- 后台自动同步最新数据

（技术信息）
- 本程序已进行数字签名（如签名成功）
- 使用 PyInstaller 打包
- 无恶意代码，开源可查

（问题反馈）
如有任何问题，请检查：
1. 是否以管理员权限运行
2. 防火墙是否阻止网络访问
3. Python 依赖是否完整安装
"""
    readme.write_text(readme_content, encoding='utf-8')
    print_check(f"使用文档已生成: {readme}")


def final_cleanup(exe_dir: Path) -> Path:
    # 最终清理和优化。
    print_step("最终优化")

    # 删除不必要的调试文件
    for pattern in ["*.pdb", "*.manifest", "*.exp", "*.lib"]:
        for f in exe_dir.glob(pattern):
            f.unlink()
            print_check(f"已删除：{f.name}")

    bundle_temp_dir = BUILD_DIR / "_bundle_static"
    if bundle_temp_dir.exists():
        shutil.rmtree(bundle_temp_dir, ignore_errors=True)

    # 重命名输出目录为更清晰的名称。
    base_name = f"Hextech_伴生系统_{datetime.now().strftime('%Y%m%d')}"
    final_dir = DIST_DIR / base_name
    suffix = 1
    while final_dir.exists() and final_dir != exe_dir:
        final_dir = DIST_DIR / f"{base_name}_{suffix}"
        suffix += 1

    if exe_dir == final_dir:
        return final_dir

    try:
        shutil.move(str(exe_dir), str(final_dir))
        print_check(f"输出目录：{final_dir}")
        return final_dir
    except PermissionError as exc:
        print_warn(f"目录重命名失败，尝试复制输出目录：{exc}")
    except OSError as exc:
        print_warn(f"目录移动失败，尝试复制输出目录：{exc}")

    try:
        shutil.copytree(exe_dir, final_dir)
        print_warn(f"已改为复制模式输出目录：{final_dir}")
        return final_dir
    except OSError as exc:
        print_warn(f"复制输出目录也失败，保留原始目录：{exc}")
        print_warn(f"请直接使用原始输出目录：{exe_dir}")
        return exe_dir


def main():
    print("\n" + "="*60)
    print("  Hextech 伴生系统打包程序")
    print(f"  构建时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    try:
        # 1. 清理
        cleanup()

        # 1.1 准备最小资源集
        _prepare_runtime_bundle()

        # 2. 生成版本信息
        version_file = generate_version_info()

        # 3. 构建可执行文件
        exe_dir = build_exe(version_file)

        # 4. 数字签名（如果可用）
        signed = sign_exe(exe_dir)

        # 5. 创建使用文档
        create_readme(exe_dir)

        # 6. 最终优化
        final_dir = final_cleanup(exe_dir)

        # 完成
        print_step("打包完成")
        print(f"  输出目录：{final_dir}")
        print(f"  主程序：{final_dir / 'Hextech伴生终端.exe'}")
        print(f"\n  {'[成功]' if signed else '[警告]'} {'已签名' if signed else '未签名（请手动签名或添加信任）'}")
        print("\n  提示：如需手动签名，请使用以下命令：")
        print("    signtool sign /f certificate.pfx /p password /tr http://timestamp.digicert.com /td SHA256 /fd SHA256 Hextech伴生终端.exe")

    except Exception as e:
        print_error(f"打包失败：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
