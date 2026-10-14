"""
流式输出取消模块。

在 LLM 流式响应期间，通过 TUI Ctrl+C 或 Escape 设置取消信号，
通知流式生成器中断。
"""

import threading

from system.tui_app import TuiRegion, post_tui

_cancel_requested = False
_response_active = False
_terminal_active_count = 0
_terminal_cancel_event = threading.Event()
_terminal_lock = threading.Lock()
_cancel_available_callbacks: list = []


def _notify_cancel_available_changed() -> None:
    for callback in list(_cancel_available_callbacks):
        callback()


def add_cancel_available_callback(callback) -> None:
    if callback not in _cancel_available_callbacks:
        _cancel_available_callbacks.append(callback)


def remove_cancel_available_callback(callback) -> None:
    try:
        _cancel_available_callbacks.remove(callback)
    except ValueError:
        pass


def cancel_current_response():
    global _cancel_requested
    if _response_active:
        _cancel_requested = True
        post_tui(TuiRegion.STATUS, "⚠️ 已取消当前响应")
        return True
    with _terminal_lock:
        terminal_active = _terminal_active_count > 0
        if terminal_active:
            _terminal_cancel_event.set()
    if terminal_active:
        post_tui(TuiRegion.STATUS, "⚠️ 已取消当前终端命令")
        return True
    return False


def start_terminal_command() -> None:
    global _terminal_active_count
    with _terminal_lock:
        if _terminal_active_count == 0:
            _terminal_cancel_event.clear()
        _terminal_active_count += 1
    _notify_cancel_available_changed()


def stop_terminal_command() -> None:
    global _terminal_active_count
    with _terminal_lock:
        _terminal_active_count = max(_terminal_active_count - 1, 0)
        if _terminal_active_count == 0:
            _terminal_cancel_event.clear()
    _notify_cancel_available_changed()


def is_terminal_cancelled() -> bool:
    return _terminal_cancel_event.is_set()


def start_cancel_listener():
    """启动取消监听。在流式输出前调用。"""
    global _cancel_requested, _response_active
    _cancel_requested = False
    _response_active = True
    _notify_cancel_available_changed()


def stop_cancel_listener():
    """停止取消监听并清除本次响应的取消信号。"""
    global _cancel_requested, _response_active
    _response_active = False
    _cancel_requested = False
    _notify_cancel_available_changed()


def is_cancelled() -> bool:
    """检查是否已被取消。"""
    return _cancel_requested


def is_cancel_available() -> bool:
    """是否存在可以被 Ctrl+C 或 Escape 取消的响应或终端命令。"""
    if _response_active:
        return True
    with _terminal_lock:
        return _terminal_active_count > 0


def reset_cancel():
    """重置取消标志。"""
    global _cancel_requested, _terminal_active_count, _response_active
    _cancel_requested = False
    _response_active = False
    with _terminal_lock:
        _terminal_active_count = 0
        _terminal_cancel_event.clear()
    _notify_cancel_available_changed()
