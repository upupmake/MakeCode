"""
自动更新模块 — 提供版本检查、下载、校验与升级启动功能。
"""

import hashlib
import json
import logging
import os
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

from version import CURRENT_VERSION, VERSION_CHECK_URL, DOWNLOAD_URL

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 8192


def _parse_version(v: str) -> tuple:
    """将版本号字符串解析为可比较的元组，例如 '1.2.3' -> (1, 2, 3)。"""
    return tuple(int(x) for x in v.strip().split("."))


def check_update(*, raise_errors: bool = False) -> dict | None:
    """
    从 VERSION_CHECK_URL 获取 version.json 并与 CURRENT_VERSION 比较。

    version.json 期望格式：
        {
            "version": "1.1.0",
            "download_url": "...",
            "sha256": "abc123...",
            "release_log": "..."
        }

    有更新返回版本信息字典，无更新返回 None。默认静默吞掉检查错误；raise_errors=True 时抛出异常。
    """
    try:
        # 创建不验证 SSL 证书的上下文（解决打包后证书路径问题）
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(VERSION_CHECK_URL, headers={"User-Agent": "Agent-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("检查更新失败: %s", exc)
        if raise_errors:
            raise RuntimeError(f"检查更新失败: {exc}") from exc
        return None

    remote_version = data.get("version", "")
    if not remote_version:
        logger.warning("version.json 缺少 version 字段")
        if raise_errors:
            raise ValueError("version.json 缺少 version 字段")
        return None

    try:
        if _parse_version(remote_version) > _parse_version(CURRENT_VERSION):
            return data
    except (ValueError, TypeError) as exc:
        logger.warning("版本号解析失败: %s", exc)
        if raise_errors:
            raise ValueError(f"版本号解析失败: {exc}") from exc

    return None


def download_update(version_info: dict, progress_callback=None) -> Path | None:
    """
    下载新版本 exe 到临时目录，校验 SHA256 后返回文件路径。

    Args:
        version_info: check_update() 返回的版本信息字典。
        progress_callback: 可选回调，签名为 (downloaded: int, total: int | None) -> None。

    Returns:
        下载文件的 Path，失败返回 None。
    """
    url = version_info.get("download_url") or DOWNLOAD_URL
    expected_sha256 = version_info.get("sha256", "")

    tmp_dir = tempfile.mkdtemp(prefix="agent_update_")
    tmp_file = Path(tmp_dir) / "MakeCode_update.exe"

    try:
        # 创建不验证 SSL 证书的上下文（解决打包后证书路径问题）
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "Agent-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=300, context=ssl_ctx) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            downloaded = 0
            sha = hashlib.sha256()

            with open(tmp_file, "wb") as fp:
                while True:
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    fp.write(chunk)
                    sha.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)
    except (urllib.error.URLError, OSError) as exc:
        logger.error("下载更新失败: %s", exc)
        return None

    if expected_sha256 and sha.hexdigest() != expected_sha256:
        logger.error("SHA256 校验失败: 期望 %s, 实际 %s", expected_sha256, sha.hexdigest())
        return None

    logger.info("更新下载完成: %s", tmp_file)
    return tmp_file


def launch_updater(new_exe_path: Path) -> None:
    """
    释放 updater.exe 到临时目录并启动，然后退出主程序。

    updater.exe 接收参数：
        --exe-path <当前exe路径>
        --new-file <新版本文件路径>
        --pid <当前进程PID>
    """
    # 定位 updater.exe：优先 PyInstaller 打包资源，其次 importlib.resources
    updater_path = _extract_updater_resource()

    current_exe = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()
    pid = os.getpid()

    cmd = [
        str(updater_path),
        "--exe-path", str(current_exe),
        "--new-file", str(new_exe_path),
        "--pid", str(pid),
    ]

    logger.info("启动更新程序: %s", cmd)
    subprocess.Popen(cmd, close_fds=True)
    os._exit(0)


def _extract_updater_resource() -> Path:
    """从打包资源或模块资源中提取 updater.exe 到临时目录。"""
    tmp_dir = tempfile.mkdtemp(prefix="updater_res_")
    dest = Path(tmp_dir) / "updater.exe"

    # PyInstaller 打包环境
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        src = Path(meipass) / "updater.exe"
        if src.exists():
            import shutil
            shutil.copy2(src, dest)
            return dest

    # importlib.resources（Python 3.9+）
    try:
        import importlib.resources as resources
        ref = resources.files("resources").joinpath("updater.exe")
        if ref.is_file():
            dest.write_bytes(ref.read_bytes())
            return dest
    except (ModuleNotFoundError, TypeError, FileNotFoundError):
        pass

    # 开发模式：从项目根目录查找
    project_root = Path(__file__).resolve().parent.parent
    for candidate in [
        project_root / "updater.exe",
        project_root / "resources" / "updater.exe",
    ]:
        if candidate.exists():
            import shutil
            shutil.copy2(candidate, dest)
            return dest

    raise FileNotFoundError("无法找到 updater.exe 资源文件")


def check_and_update(silent: bool = True) -> bool:
    """
    便捷函数：检查更新 → 下载 → 启动更新程序。

    Args:
        silent: True 时静默检查；False 时打印过程信息。

    Returns:
        有可用更新返回 True，无更新或失败返回 False。
    """
    if not silent:
        print(f"当前版本: {CURRENT_VERSION}")
        print("正在检查更新...")

    version_info = check_update()
    if version_info is None:
        if not silent:
            print("当前已是最新版本。")
        return False

    remote_ver = version_info.get("version", "未知")
    release_log = version_info.get("release_log", "")

    if not silent:
        print(f"发现新版本: {remote_ver}")
        if release_log:
            print(f"更新说明: {release_log}")

    def _progress(downloaded: int, total: int | None) -> None:
        if not silent:
            if total:
                pct = downloaded / total * 100
                print(f"\r下载进度: {pct:.1f}%  ({downloaded}/{total})", end="", flush=True)
            else:
                print(f"\r已下载: {downloaded} 字节", end="", flush=True)

    if not silent:
        print("正在下载更新...")

    exe_path = download_update(version_info, progress_callback=_progress)
    if exe_path is None:
        if not silent:
            print("\n下载或校验失败。")
        return False

    if not silent:
        print()  # 换行
        print("下载完成，准备应用更新...")

    launch_updater(exe_path)
    # launch_updater 内部会 sys.exit(0)，正常流程不会到达此处
    return True
