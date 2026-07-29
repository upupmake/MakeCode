"""
自动更新模块 — 提供版本检查、下载、校验与升级启动功能。
"""

import hashlib
import json
import logging
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

import certifi

from version import CURRENT_VERSION, VERSION_CHECK_URL, DOWNLOAD_URL

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 8192


def _current_platform_key() -> str | None:
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        return None
    if sys.platform == "win32":
        return "windows-x86_64"
    if sys.platform.startswith("linux"):
        return "linux-x86_64"
    return None


UPDATE_PLATFORM_KEY = _current_platform_key()
AUTO_UPDATE_SUPPORTED = UPDATE_PLATFORM_KEY is not None


def _create_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


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
        req = urllib.request.Request(VERSION_CHECK_URL, headers={"User-Agent": "MakeCode-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_create_ssl_context()) as resp:
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


def _get_platform_asset(version_info: dict) -> dict:
    if UPDATE_PLATFORM_KEY is None:
        raise RuntimeError("当前平台不支持应用内自动更新")

    platforms = version_info.get("platforms")
    if isinstance(platforms, dict):
        asset = platforms.get(UPDATE_PLATFORM_KEY)
        if not isinstance(asset, dict):
            raise ValueError(f"version.json 缺少 {UPDATE_PLATFORM_KEY} 平台信息")
        return asset

    if UPDATE_PLATFORM_KEY == "windows-x86_64":
        return version_info
    raise ValueError("version.json 缺少 platforms 平台信息")


def download_update(version_info: dict, progress_callback=None) -> Path | None:
    """
    下载当前平台的 onedir 完整 ZIP，校验大小和 SHA256 后返回文件路径。

    Args:
        version_info: check_update() 返回的版本信息字典。
        progress_callback: 可选回调，签名为 (downloaded: int, total: int | None) -> None。

    Returns:
        下载文件的 Path，失败返回 None。
    """
    asset = _get_platform_asset(version_info)
    url = asset.get("download_url")
    if not url and UPDATE_PLATFORM_KEY == "windows-x86_64":
        url = DOWNLOAD_URL
    expected_sha256 = asset.get("sha256", "")
    expected_size = asset.get("size")
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise ValueError("version.json 中的 download_url 必须使用 HTTPS")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("version.json 缺少有效的 sha256")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise ValueError("version.json 缺少有效的 size")

    archive_name = {
        "windows-x86_64": "MakeCode-Windows-X64.zip",
        "linux-x86_64": "MakeCode-Linux-X64.zip",
    }[UPDATE_PLATFORM_KEY]
    tmp_dir = tempfile.mkdtemp(prefix="makecode_update_")
    tmp_file = Path(tmp_dir) / archive_name

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MakeCode-Updater/1.0"})
        with urllib.request.urlopen(req, timeout=300, context=_create_ssl_context()) as resp:
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
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    if downloaded != expected_size:
        logger.error("文件大小校验失败: 期望 %d, 实际 %d", expected_size, downloaded)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    if sha.hexdigest().lower() != expected_sha256.lower():
        logger.error("SHA256 校验失败: 期望 %s, 实际 %s", expected_sha256, sha.hexdigest())
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    logger.info("更新下载完成: %s", tmp_file)
    return tmp_file


def launch_updater(update_archive: Path) -> None:
    """释放内置 updater，启动完整目录更新，然后退出整个主进程。"""
    if not AUTO_UPDATE_SUPPORTED:
        raise RuntimeError("当前平台不支持应用内自动更新，请从 GitHub Release 下载对应平台版本")

    updater_path = _extract_updater_resource()

    current_exe = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()
    pid = os.getpid()

    cmd = [
        str(updater_path),
        "--install-dir", str(current_exe.parent),
        "--archive", str(update_archive.resolve()),
        "--pid", str(pid),
    ]

    logger.info("启动更新程序: %s", cmd)
    subprocess.Popen(cmd, cwd=str(updater_path.parent), close_fds=True)
    os._exit(0)


def _extract_updater_resource() -> Path:
    """从打包资源或模块资源中提取 updater 到临时目录。"""
    resource_name = "updater.exe" if sys.platform == "win32" else "updater"
    tmp_dir = tempfile.mkdtemp(prefix="updater_res_")
    dest = Path(tmp_dir) / resource_name

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        src = Path(meipass) / resource_name
        if src.exists():
            shutil.copy2(src, dest)
            if sys.platform.startswith("linux"):
                dest.chmod(dest.stat().st_mode | 0o700)
            return dest

    try:
        import importlib.resources as resources
        ref = resources.files("resources").joinpath(resource_name)
        if ref.is_file():
            dest.write_bytes(ref.read_bytes())
            if sys.platform.startswith("linux"):
                dest.chmod(dest.stat().st_mode | 0o700)
            return dest
    except (ModuleNotFoundError, TypeError, FileNotFoundError):
        pass

    project_root = Path(__file__).resolve().parent.parent
    for candidate in [
        project_root / resource_name,
        project_root / "dist" / resource_name,
        project_root / "resources" / resource_name,
    ]:
        if candidate.exists():
            shutil.copy2(candidate, dest)
            if sys.platform.startswith("linux"):
                dest.chmod(dest.stat().st_mode | 0o700)
            return dest

    raise FileNotFoundError(f"无法找到 {resource_name} 资源文件")


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
