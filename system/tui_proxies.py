"""Lazy proxies for TUI entry points.

Modules that only need to *emit* events must not import Textual at module load
time. Headless mode sets ``set_headless_mode(True)`` so UI-only helpers become
no-ops or safe defaults instead of importing Textual.
"""

from __future__ import annotations

_headless_mode = False


def set_headless_mode(enabled: bool = True) -> None:
    global _headless_mode
    _headless_mode = enabled


def post_tui(
    region,
    payload=None,
    *,
    clear: bool = False,
    tail: bool = False,
    active: bool | None = None,
    collapsible_title: str | None = None,
    collapsible_open: bool = False,
    collapsible_close: bool = False,
    collapsible_kind: str = "reasoning",
    tail_after=None,
):
    if not _headless_mode:
        from system.tui_app import post_tui as _post_tui

        return _post_tui(
            region,
            payload,
            clear=clear,
            tail=tail,
            active=active,
            collapsible_title=collapsible_title,
            collapsible_open=collapsible_open,
            collapsible_close=collapsible_close,
            collapsible_kind=collapsible_kind,
            tail_after=tail_after,
        )

    from system.output_bus import OUTPUT_BUS
    from system.tui_types import TuiEvent, TuiRegion

    event = TuiEvent(
        TuiRegion(region),
        payload,
        clear,
        tail,
        active,
        collapsible_title,
        collapsible_open,
        collapsible_close,
        collapsible_kind,
        tail_after,
    )
    OUTPUT_BUS.emit(event)


def refresh_status(*args, **kwargs) -> None:
    if _headless_mode:
        return
    from system.tui_app import refresh_status as _refresh_status

    return _refresh_status(*args, **kwargs)


def set_client_request_active(active: bool, request_id: int | None = None) -> None:
    if _headless_mode:
        return
    from system.tui_app import set_client_request_active as _set_client_request_active

    return _set_client_request_active(active, request_id=request_id)


def set_client_request_retry(
    request_id: int,
    retry_count: int,
    max_retries: int,
) -> None:
    if _headless_mode:
        return
    from system.tui_app import set_client_request_retry as _set_client_request_retry

    return _set_client_request_retry(request_id, retry_count, max_retries)


def choose_delegate_tasks_tui(*args, **kwargs):
    if _headless_mode:
        return "orchestrator"
    from system.tui_app import choose_delegate_tasks_tui as _choose_delegate_tasks_tui

    return _choose_delegate_tasks_tui(*args, **kwargs)


def choose_tui(*args, **kwargs):
    if _headless_mode:
        return "<cancelled>"
    from system.tui_app import choose_tui as _choose_tui

    return _choose_tui(*args, **kwargs)


def is_simple_mode() -> bool:
    if _headless_mode:
        return False
    from system.tui_app import is_simple_mode as _is_simple_mode

    return _is_simple_mode()
