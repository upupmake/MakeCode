from __future__ import annotations

import base64
import copy
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any


IMAGE_PLACEHOLDER_PATTERN = re.compile(
    r"\[\[image:id=(?P<id>img_[0-9a-f]{32})\]\]"
)
IMAGE_ATTACHMENT_PATTERN = re.compile(r"^img_[0-9a-f]{32}$")
SUPPORTED_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _attachment_root(conversation_root: Path) -> Path:
    return conversation_root / "attachments"


def _attachment_path(conversation_root: Path, attachment_id: str, filename: str) -> Path:
    if not IMAGE_ATTACHMENT_PATTERN.fullmatch(attachment_id):
        raise ValueError(f"Invalid image attachment ID: {attachment_id}")
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise ValueError(f"Invalid image attachment filename: {filename}")
    return _attachment_root(conversation_root) / f"{attachment_id}_{safe_name}"


def _image_media_type(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type not in SUPPORTED_IMAGE_TYPES:
        raise ValueError(f"Unsupported image type: {path.name}")
    return media_type


def _attachment_record(path: Path, attachment_id: str) -> dict[str, str]:
    prefix = f"{attachment_id}_"
    filename = path.name.removeprefix(prefix)
    if filename == path.name:
        raise ValueError(f"Invalid image attachment filename: {path}")
    return {
        "type": "image",
        "attachment_id": attachment_id,
        "filename": filename,
        "media_type": _image_media_type(path),
    }


def store_image_bytes_attachment(
    conversation_root: Path,
    data: bytes,
    filename: str,
    media_type: str,
) -> dict[str, str]:
    if not isinstance(data, bytes) or not data:
        raise ValueError("Image data is empty")
    if media_type not in SUPPORTED_IMAGE_TYPES:
        raise ValueError(f"Unsupported image type: {media_type}")
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise ValueError(f"Invalid image attachment filename: {filename}")
    attachment_id = f"img_{uuid.uuid4().hex}"
    target_dir = _attachment_root(conversation_root)
    if target_dir.is_symlink() or (target_dir.exists() and not target_dir.is_dir()):
        raise ValueError(f"Invalid image attachment directory: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _attachment_path(conversation_root, attachment_id, filename)
    target.write_bytes(data)
    return {
        "type": "image",
        "attachment_id": attachment_id,
        "filename": filename,
        "media_type": media_type,
    }


def resolve_image_attachment(conversation_root: Path, block: dict[str, Any]) -> tuple[dict[str, str], bytes]:
    attachment_id = block.get("attachment_id")
    filename = block.get("filename")
    if not isinstance(attachment_id, str) or not isinstance(filename, str):
        raise ValueError("Invalid image attachment block")
    path = _attachment_path(conversation_root, attachment_id, filename)
    attachment_root = _attachment_root(conversation_root)
    if attachment_root.is_symlink() or (attachment_root.exists() and not attachment_root.is_dir()):
        raise ValueError(f"Invalid image attachment directory: {attachment_root}")
    root = attachment_root.resolve()
    if path.is_symlink() or path.parent.resolve() != root or not path.is_file():
        raise ValueError(f"Invalid image attachment path: {path}")
    media_type = block.get("media_type")
    if media_type not in SUPPORTED_IMAGE_TYPES or media_type != _image_media_type(path):
        raise ValueError(f"Invalid image attachment media type: {path}")
    return {
        "type": "image",
        "attachment_id": attachment_id,
        "filename": filename,
        "media_type": media_type,
    }, path.read_bytes()


def image_placeholder_text(block: dict[str, Any]) -> str:
    return f"[图片：{block.get('filename') or block.get('attachment_id') or '未命名'}]"


def remove_image_placeholders(text: str) -> str:
    text = re.sub(r"\[\[image:id=[^\]]+\]\]", "", text)
    return re.sub(r"\[图片：[^\]]+\]", "", text)


def image_reference_marker(block: dict[str, Any]) -> str:
    attachment_id = block.get("attachment_id")
    if not isinstance(attachment_id, str) or not IMAGE_ATTACHMENT_PATTERN.fullmatch(attachment_id):
        raise ValueError("Invalid image attachment block")
    return f"[[image:id={attachment_id}]]"


def text_content_from_parts(parts: list[dict[str, Any]]) -> str:
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )


def text_only_messages(messages: list[dict[str, Any]], *, include_image_placeholders: bool = False) -> list[dict[str, Any]]:
    projected = copy.deepcopy(messages)
    for message in projected:
        content = message.get("content")
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "image" and include_image_placeholders:
                        text_parts.append(image_placeholder_text(block))
            message["content"] = "".join(text_parts)
        content_blocks = message.get("content_blocks")
        if isinstance(content_blocks, list):
            message["content_blocks"] = [
                block for block in content_blocks
                if not isinstance(block, dict) or block.get("type") != "image"
            ]
        metadata = message.get("message_metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("display_content"), str):
            metadata["display_content"] = remove_image_placeholders(metadata["display_content"])
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = remove_image_placeholders(message["content"])
    return projected


def image_blocks_from_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [
        block for block in content
        if isinstance(block, dict) and block.get("type") == "image"
    ]


def strip_images_in_place(messages: list[dict[str, Any]]) -> None:
    messages[:] = text_only_messages(messages)


def image_data_uri(conversation_root: Path, block: dict[str, Any]) -> str:
    record, data = resolve_image_attachment(conversation_root, block)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{record['media_type']};base64,{encoded}"


def parse_image_placeholders(
    text: str,
    conversation_root: Path,
) -> tuple[str, list[dict[str, str]]]:
    parts: list[dict[str, str]] = []
    position = 0
    for match in IMAGE_PLACEHOLDER_PATTERN.finditer(text):
        if match.start() > position:
            parts.append({"type": "text", "text": text[position:match.start()]})
        attachment_id = match.group("id")
        attachment_dir = conversation_root / "attachments"
        matches = list(attachment_dir.glob(f"{attachment_id}_*"))
        if len(matches) != 1:
            raise ValueError(f"Image attachment not found: {attachment_id}")
        block = _attachment_record(matches[0], attachment_id)
        resolve_image_attachment(conversation_root, block)
        parts.append(block)
        position = match.end()
    if position < len(text):
        parts.append({"type": "text", "text": text[position:]})
    if not any(part.get("type") == "image" for part in parts):
        return text, []
    display_text = "".join(
        part.get("text", "") if part.get("type") == "text" else image_placeholder_text(part)
        for part in parts
    )
    return display_text, parts
