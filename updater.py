"""独立更新器：安全解压并事务替换 Windows onedir 应用目录。"""

import argparse
import ctypes
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("updater")


def wait_process_exit(pid: int, timeout: float) -> bool:
    """等待指定 PID 的进程退出，返回 True 表示已退出，False 表示超时或失败。"""
    if pid <= 0:
        log.error("非法 PID: %s", pid)
        return False

    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == 87:
                return True
            log.warning("OpenProcess 失败 (error=%d)，降级到轮询等待", err)
        else:
            try:
                return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
            finally:
                kernel32.CloseHandle(handle)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.5)
    return False


def retry_file_op(func, retries=5, delay=1.0):
    """带重试的文件操作，对抗杀软文件锁定。"""
    for attempt in range(retries):
        try:
            return func()
        except OSError as exc:
            if attempt == retries - 1:
                raise
            log.warning("操作失败，%.1f秒后重试 (%s)", delay, exc)
            time.sleep(delay)


def extract_update_archive(archive: Path, staging_dir: Path) -> Path:
    """安全解压更新包，返回包内唯一的 MakeCode 应用目录。"""
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if not members:
            raise ValueError("更新包为空")

        for member in members:
            path = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if (
                path.is_absolute()
                or not path.parts
                or "\\" in member.filename
                or ":" in member.filename
                or ".." in path.parts
                or path.parts[0] != "MakeCode"
                or stat.S_ISLNK(mode)
            ):
                raise ValueError(f"更新包包含不安全路径: {member.filename}")

        bundle.extractall(staging_dir)

    app_dir = staging_dir / "MakeCode"
    if not (app_dir / "MakeCode.exe").is_file() or not (app_dir / "_internal").is_dir():
        raise ValueError("更新包结构无效：缺少 MakeCode.exe 或 _internal")
    return app_dir


def wait_for_ready(process, ready_file: Path, timeout: float = 30) -> None:
    """等待新版报告启动成功；提前退出或超时均视为失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.is_file():
            return
        if process.poll() is not None:
            raise RuntimeError(f"新版进程提前退出，退出码 {process.returncode}")
        time.sleep(0.2)
    process.terminate()
    process.wait(timeout=5)
    raise TimeoutError("新版启动确认超时")


def install_update(archive: Path, install_dir: Path) -> None:
    """用同级目录切换安装更新；任何失败都会恢复旧目录。"""
    parent = install_dir.parent
    staging_root = parent / f".{install_dir.name}.staging.{os.getpid()}"
    backup_dir = parent / f".{install_dir.name}.backup.{os.getpid()}"
    ready_file = parent / f".{install_dir.name}.ready.{os.getpid()}"
    staged_app = None
    old_install_moved = False

    if staging_root.exists() or backup_dir.exists() or ready_file.exists():
        raise FileExistsError("更新暂存目录已存在")

    try:
        staging_root.mkdir()
        staged_app = extract_update_archive(archive, staging_root)
        retry_file_op(lambda: os.replace(install_dir, backup_dir))
        old_install_moved = True

        user_data = backup_dir / ".makecode"
        if user_data.exists():
            retry_file_op(lambda: shutil.copytree(user_data, staged_app / ".makecode"))

        retry_file_op(lambda: os.replace(staged_app, install_dir))
        env = os.environ.copy()
        env["MAKECODE_UPDATE_READY_FILE"] = str(ready_file)
        process = subprocess.Popen(
            [str(install_dir / "MakeCode.exe")], cwd=str(install_dir), close_fds=True, env=env
        )
        wait_for_ready(process, ready_file)
    except Exception:
        if old_install_moved:
            if install_dir.exists():
                shutil.rmtree(install_dir, ignore_errors=True)
            if backup_dir.exists():
                retry_file_op(lambda: os.replace(backup_dir, install_dir))
        raise
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)
    finally:
        ready_file.unlink(missing_ok=True)
        shutil.rmtree(staging_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="MakeCode 目录更新器")
    parser.add_argument("--install-dir", required=True, type=Path, help="当前 MakeCode 应用目录")
    parser.add_argument("--archive", required=True, type=Path, help="已校验的完整更新 ZIP")
    parser.add_argument("--pid", required=True, type=int, help="主程序进程 ID")
    args = parser.parse_args()

    install_dir = args.install_dir.resolve()
    archive = args.archive.resolve()
    if not (install_dir / "MakeCode.exe").is_file():
        parser.error("安装目录中缺少 MakeCode.exe")
    if not archive.is_file():
        parser.error("更新 ZIP 不存在")

    log.info("等待主程序 (PID %d) 退出", args.pid)
    if not wait_process_exit(args.pid, timeout=30):
        raise SystemExit("等待主程序退出超时，更新中止")

    try:
        install_update(archive, install_dir)
    except Exception:
        log.exception("更新失败，已尝试恢复旧版本")
        raise SystemExit(1)
    finally:
        try:
            archive.unlink(missing_ok=True)
            archive.parent.rmdir()
        except OSError:
            pass

    log.info("更新完成")


if __name__ == "__main__":
    main()
