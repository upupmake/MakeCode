import os
import threading
from pathlib import Path


class FileAccessController:
    """全局文件访问控制器，管理并发锁和获取物理真实修改时间"""

    def __init__(self):
        # 确保多个子代理并发执行读写时的线程安全
        self.global_lock = threading.RLock()

    def get_real_mtime(self, filepath: Path) -> float:
        """获取物理文件的真实最后修改时间"""
        if filepath.exists():
            return os.path.getmtime(filepath)
        return 0.0


# 全局单例
GLOBAL_FILE_CONTROLLER = FileAccessController()


class AgentFileAccess:
    """每个智能体专属的文件访问记录器"""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
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
                f"🔴 拦截: 试图编辑未读取的文件 '{path}'。请务必先使用 RunRead 读取该文件以获取最新内容！",
            )

        recorded_mtime = self.visited_files[path]
        if recorded_mtime != current_mtime:
            return (
                False,
                f"🔴 拦截: 文件 '{path}' 在你上次读取后已被其他程序或智能体修改（或你刚修改过但未重新读取）。为了防止覆盖他人代码或产生幻觉，请必须重新使用 RunRead 读取最新内容后再进行 RunEdit！",
            )

        return True, ""
