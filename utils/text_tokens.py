import os
import re
import sys
from pathlib import Path

try:
    import tiktoken

    if getattr(sys, "frozen", False):
        _base_path = Path(sys._MEIPASS)
    else:
        _base_path = Path(__file__).parent.parent

    _local_cache = _base_path / "tiktoken_cache"
    if _local_cache.exists():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(_local_cache)

    _ENCODER = tiktoken.get_encoding("o200k_base")
except ImportError:
    _ENCODER = None


_USE_DEFAULT_ENCODER = object()


def estimate_text_tokens(text: str, encoder=_USE_DEFAULT_ENCODER) -> int:
    active_encoder = _ENCODER if encoder is _USE_DEFAULT_ENCODER else encoder
    if active_encoder:
        return len(active_encoder.encode(text, disallowed_special=()))
    return len(text)


def _decode_token_slice(tokens: list[int], encoder=_USE_DEFAULT_ENCODER) -> str:
    active_encoder = _ENCODER if encoder is _USE_DEFAULT_ENCODER else encoder
    if not active_encoder:
        raise RuntimeError("Token decoder is unavailable")
    return active_encoder.decode_bytes(tokens).decode("utf-8", errors="ignore")


def truncate_text_by_tokens(
        text: str,
        max_tokens: int,
        edge_tokens: int,
        marker: str,
        existing_marker_pattern: re.Pattern[str],
        tail_tokens: int | None = None,
        encoder=_USE_DEFAULT_ENCODER,
) -> str:
    """Truncate text by model tokens without counting existing truncation markers."""
    if not text:
        return text

    active_encoder = _ENCODER if encoder is _USE_DEFAULT_ENCODER else encoder
    tail_tokens = edge_tokens if tail_tokens is None else tail_tokens
    payload = existing_marker_pattern.sub("", text)
    token_count = estimate_text_tokens(payload, active_encoder)
    if token_count <= max_tokens:
        return text

    if active_encoder:
        tokens = active_encoder.encode(payload, disallowed_special=())
        head = _decode_token_slice(tokens[:edge_tokens], active_encoder)
        tail = _decode_token_slice(tokens[-tail_tokens:], active_encoder)
        omitted_tokens = max(
            0,
            token_count
            - estimate_text_tokens(head, active_encoder)
            - estimate_text_tokens(tail, active_encoder),
        )
    else:
        head = payload[:edge_tokens]
        tail = payload[-tail_tokens:]
        omitted_tokens = max(0, token_count - len(head) - len(tail))

    marker_text = marker.format(omitted_tokens=omitted_tokens)
    return head + marker_text + tail
