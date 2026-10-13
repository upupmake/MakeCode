"""TUI 渲染模式控制。

simple 模式是低渲染开销模式：隐藏次要面板、关闭实时尾部渲染、
把 Markdown 排版降级为纯文本。

该状态**只作用于当前进程生命周期**，不写入任何配置文件，
下次启动一律回到完整模式。需要长期生效请用启动参数 ``--simple``。

本模块刻意不导入项目内其它模块，避免循环导入。
"""

import threading

_lock = threading.Lock()
_simple_mode = False


def is_simple_mode() -> bool:
    """当前是否为 simple 模式。"""
    return _simple_mode


def set_simple_mode(enabled: bool) -> bool:
    """设置 simple 模式，返回设置后的状态。"""
    global _simple_mode
    with _lock:
        _simple_mode = bool(enabled)
        return _simple_mode


def toggle_simple_mode() -> bool:
    """切换 simple 模式，返回切换后的状态。"""
    global _simple_mode
    with _lock:
        _simple_mode = not _simple_mode
        return _simple_mode
