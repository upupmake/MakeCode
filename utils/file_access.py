import threading
from pathlib import Path


class FileAccessController:
    """全局文件访问控制器，管理文件级并发锁。"""

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


# 全局单例
GLOBAL_FILE_CONTROLLER = FileAccessController()
