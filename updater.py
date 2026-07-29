"""独立更新器：安全解压并事务替换 Windows/Linux onedir 应用目录。"""

import argparse
import ctypes
import logging
import os
import posixpath
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
IS_WINDOWS = os.name == "nt"


def _configure_file_logging(log_file: Path) -> None:
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(handler)


def wait_process_exit(pid: int, timeout: float) -> bool:
    """等待指定 PID 的进程退出，返回 True 表示已退出，False 表示超时或失败。"""
    if pid <= 0:
        log.error("非法 PID: %s", pid)
        return False

    if IS_WINDOWS:
        import ctypes.wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == 87:
                return True
            log.warning("OpenProcess 失败 (error=%d)，降级到轮询等待", err)
        else:
            try:
                wait_result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
                if wait_result == 0:
                    return True
                if wait_result == 258:
                    return False
                log.error("WaitForSingleObject 失败 (error=%d)", ctypes.get_last_error())
                return False
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


def _app_executable(app_dir: Path) -> Path:
    name = "MakeCode.exe" if IS_WINDOWS else "MakeCode"
    return app_dir / name


def _validate_symlink_target(path: PurePosixPath, target: str) -> None:
    target_path = PurePosixPath(target)
    normalized = PurePosixPath(posixpath.normpath(str(path.parent / target_path)))
    if (
        target_path.is_absolute()
        or "\\" in target
        or ":" in target
        or not normalized.parts
        or normalized.parts[0] != "MakeCode"
    ):
        raise ValueError(f"更新包包含不安全符号链接: {path} -> {target}")


def extract_update_archive(archive: Path, staging_dir: Path) -> Path:
    """安全解压更新包，返回包内唯一的 MakeCode 应用目录。"""
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if not members:
            raise ValueError("更新包为空")

        paths = [PurePosixPath(member.filename) for member in members]
        if len(set(paths)) != len(paths):
            raise ValueError("更新包包含重复路径")
        symlink_paths = {
            path
            for member, path in zip(members, paths)
            if stat.S_ISLNK(member.external_attr >> 16)
        }

        for member, path in zip(members, paths):
            if (
                path.is_absolute()
                or not path.parts
                or "\\" in member.filename
                or ":" in member.filename
                or ".." in path.parts
                or path.parts[0] != "MakeCode"
                or any(parent in symlink_paths for parent in path.parents)
            ):
                raise ValueError(f"更新包包含不安全路径: {member.filename}")
            if path in symlink_paths:
                if IS_WINDOWS:
                    raise ValueError(f"Windows 更新包不能包含符号链接: {member.filename}")
                target = bundle.read(member).decode("utf-8")
                _validate_symlink_target(path, target)

        for member, path in zip(members, paths):
            if path in symlink_paths:
                continue
            bundle.extract(member, staging_dir)
            if not IS_WINDOWS:
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    (staging_dir / path).chmod(mode)

        for member, path in zip(members, paths):
            if path not in symlink_paths:
                continue
            target = bundle.read(member).decode("utf-8")
            destination = staging_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, destination)

    app_dir = staging_dir / "MakeCode"
    executable = _app_executable(app_dir)
    if not executable.is_file() or not (app_dir / "_internal").is_dir():
        raise ValueError(f"更新包结构无效：缺少 {executable.name} 或 _internal")
    if not IS_WINDOWS and not os.access(executable, os.X_OK):
        raise ValueError("更新包结构无效：MakeCode 缺少执行权限")
    return app_dir


def _move_entries(source: Path, destination: Path, skip_names=()) -> None:
    destination.mkdir(exist_ok=True)
    for entry in source.iterdir():
        if entry.name in skip_names:
            continue
        retry_file_op(lambda entry=entry: os.replace(entry, destination / entry.name))


def _remove_entries(directory: Path, skip_names=()) -> None:
    for entry in directory.iterdir():
        if entry.name in skip_names:
            continue
        if entry.is_dir() and not entry.is_symlink():
            retry_file_op(lambda entry=entry: shutil.rmtree(entry))
        else:
            retry_file_op(entry.unlink)


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
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
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
        if IS_WINDOWS:
            backup_dir.mkdir()
            try:
                _move_entries(install_dir, backup_dir, skip_names={".makecode"})
            except Exception:
                _move_entries(backup_dir, install_dir)
                backup_dir.rmdir()
                raise
            old_install_moved = True
            _move_entries(staged_app, install_dir, skip_names={".makecode"})
        else:
            retry_file_op(lambda: os.replace(install_dir, backup_dir))
            old_install_moved = True
            user_data = backup_dir / ".makecode"
            if user_data.exists():
                retry_file_op(lambda: shutil.copytree(user_data, staged_app / ".makecode"))
            retry_file_op(lambda: os.replace(staged_app, install_dir))

        env = os.environ.copy()
        env["MAKECODE_UPDATE_READY_FILE"] = str(ready_file)
        process = subprocess.Popen(
            [str(_app_executable(install_dir))],
            cwd=str(install_dir),
            close_fds=True,
            env=env,
        )
        wait_for_ready(process, ready_file)
    except Exception:
        if old_install_moved:
            if IS_WINDOWS:
                _remove_entries(install_dir, skip_names={".makecode"})
                if backup_dir.exists():
                    _move_entries(backup_dir, install_dir)
                    backup_dir.rmdir()
            else:
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
    executable = _app_executable(install_dir)
    if not executable.is_file():
        parser.error(f"安装目录中缺少 {executable.name}")
    if not archive.is_file():
        parser.error("更新 ZIP 不存在")

    _configure_file_logging(install_dir.parent / f".{install_dir.name}.update.log")
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
