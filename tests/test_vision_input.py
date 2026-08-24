import base64
from pathlib import Path

from utils.llm_client import build_anthropic_request_messages, sanitize_openai_messages
from utils.vision import (
    image_reference_marker,
    parse_image_placeholders,
    remove_image_placeholders,
    store_image_bytes_attachment,
    text_only_messages,
)


def _image_file(tmp_path: Path, name: str = "sample.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"png-bytes")
    return path


def test_manual_image_path_marker_is_plain_text(tmp_path):
    conversation_root = tmp_path / "conv"
    source = _image_file(tmp_path)
    text = f"before [[image:path={source}]] after"

    display, parts = parse_image_placeholders(text, conversation_root)

    assert display == text
    assert parts == []
    assert not (conversation_root / "attachments").exists()


def test_manual_filesystem_path_is_not_converted(tmp_path):
    conversation_root = tmp_path / "conv"
    source = _image_file(tmp_path)

    display, parts = parse_image_placeholders(str(source), conversation_root)

    assert display == str(source)
    assert parts == []
    assert not (conversation_root / "attachments").exists()


def test_manual_file_url_is_not_converted(tmp_path):
    conversation_root = tmp_path / "conv"
    source = _image_file(tmp_path)

    display, parts = parse_image_placeholders(source.as_uri(), conversation_root)

    assert display == source.as_uri()
    assert parts == []
    assert not (conversation_root / "attachments").exists()


def test_existing_attachment_placeholder_can_be_restored(tmp_path):
    conversation_root = tmp_path / "conv"
    block = store_image_bytes_attachment(
        conversation_root,
        b"png-bytes",
        "sample.png",
        "image/png",
    )

    display, restored = parse_image_placeholders(
        image_reference_marker(block),
        conversation_root,
    )

    assert display == "[图片：sample.png]"
    assert restored[0]["attachment_id"] == block["attachment_id"]


def test_text_only_projection_removes_images_without_mutating_history():
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "question"},
            {"type": "image", "attachment_id": "img_00000000000000000000000000000000", "filename": "x.png", "media_type": "image/png"},
        ],
    }]

    projected = text_only_messages(messages)

    assert projected == [{"role": "user", "content": "question"}]
    assert messages[0]["content"][1]["type"] == "image"
    assert remove_image_placeholders("a [图片：x.png] b [[image:id=img_00000000000000000000000000000000]]") == "a  b "


def test_post_compaction_projection_removes_images_from_all_messages():
    from utils.vision import strip_images_in_place

    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "attachment_id": "img_00000000000000000000000000000000", "filename": "x.png", "media_type": "image/png"},
            {"type": "text", "text": "question"},
        ],
        "message_metadata": {"display_content": "[图片：x.png] question"},
    }]

    strip_images_in_place(messages)

    assert messages == [{
        "role": "user",
        "content": "question",
        "message_metadata": {"display_content": " question"},
    }]


def test_openai_and_anthropic_convert_canonical_image_blocks(tmp_path):
    conversation_root = tmp_path / "conv"
    block = store_image_bytes_attachment(
        conversation_root,
        b"png-bytes",
        "sample.png",
        "image/png",
    )
    _, parts = parse_image_placeholders(
        f"question {image_reference_marker(block)}",
        conversation_root,
    )
    message = {"role": "user", "content": parts}

    openai = sanitize_openai_messages([message], conversation_root)
    _, anthropic = build_anthropic_request_messages([message], conversation_root)

    assert openai[0]["content"][0] == {"type": "text", "text": "question "}
    assert openai[0]["content"][1]["type"] == "image_url"
    assert openai[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert anthropic[0]["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"png-bytes").decode("ascii"),
        },
    }


def test_protocol_clients_drop_images_when_no_conversation_root_is_supplied(tmp_path):
    conversation_root = tmp_path / "conv"
    block = store_image_bytes_attachment(
        conversation_root,
        b"png-bytes",
        "sample.png",
        "image/png",
    )
    _, parts = parse_image_placeholders(
        f"question {image_reference_marker(block)}",
        conversation_root,
    )
    message = {"role": "user", "content": parts}

    assert sanitize_openai_messages([message])[0]["content"] == [{"type": "text", "text": "question "}]
    assert build_anthropic_request_messages([message])[1][0]["content"] == [{"type": "text", "text": "question "}]
