import os
import threading
from datetime import datetime
from pathlib import Path


class FileAccessController:
    """全局文件访问控制器，管理并发锁和获取物理真实修改时间"""

    def __init__(self):
        # 确保多个子代理并发执行读写时的线程安全，使用文件级细粒度锁
        self._dict_lock = threading.Lock()
        self._file_locks: dict[str, threading.RLock] = {}

    def get_lock(self, filepath: Path) -> threading.RLock:
        """获取特定文件的 RLock，实现细粒度并发控制"""
        abs_path = str(filepath.resolve())
        with self._dict_lock:
            if abs_path not in self._file_locks:
                self._file_locks[abs_path] = threading.RLock()
            return self._file_locks[abs_path]

    def get_real_mtime(self, filepath: Path) -> float:
        """获取物理文件的真实最后修改时间"""
        if filepath.exists():
            return os.path.getmtime(filepath)
        return 0.0


# 全局单例
GLOBAL_FILE_CONTROLLER = FileAccessController()


class AgentFileAccess:
    """每个智能体专属的文件访问记录器"""

    def __init__(self):
        # key: 相对路径字符串, value: 最后一次看到的 mtime
        self.visited_files: dict[str, float] = {}

    def record_access(self, path: str, mtime: float):
        """在 RunRead 或 RunWrite 成功后记录文件的 mtime"""
        self.visited_files[path] = mtime

    def can_edit(self, path: str, current_mtime: float) -> tuple[bool, str]:
        """在 RunEdit 前严格检查是否允许修改"""
        if path not in self.visited_files:
            return (
                False,
                f"🔴 拦截: 试图编辑未读取的文件 '{path}'。请务必先使用 RunRead 读取该文件以获取最新内容。",
            )

        recorded_mtime = self.visited_files[path]
        if recorded_mtime != current_mtime:
            # 格式化时间戳为毫秒级 UTC 时间，例如：2026-04-04T07:41:58.823Z
            def _fmt(ts: float) -> str:
                return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            error_msg = (
                f"🔴 拦截: 文件 '{path}' 在你上次读取后已被其他程序或智能体修改（或你刚修改过但未重新读取）。\n"
                f"Last modification: {_fmt(current_mtime)}\n"
                f"Last read: {_fmt(recorded_mtime)}\n"
                f"必须重新使用 RunRead 读取最新内容后再进行 RunEdit 。"
            )
            return False, error_msg
        return True, ""
