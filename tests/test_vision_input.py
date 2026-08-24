import base64
from pathlib import Path

import pytest

from utils.llm_client import build_anthropic_request_messages, sanitize_openai_messages
from utils.vision import (
    parse_image_placeholders,
    parse_pasted_image_references,
    remove_image_placeholders,
    text_only_messages,
)


def _image_file(tmp_path: Path, name: str = "sample.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"png-bytes")
    return path


def test_image_placeholder_is_copied_and_parsed_in_input_order(tmp_path):
    conversation_root = tmp_path / "conv"
    source = _image_file(tmp_path)

    display, parts = parse_image_placeholders(
        f"before [[image:path={source}]] after",
        conversation_root,
    )

    assert display == "before [图片：sample.png] after"
    assert [part["type"] for part in parts] == ["text", "image", "text"]
    block = parts[1]
    attachment = conversation_root / "attachments" / f"{block['attachment_id']}_sample.png"
    assert attachment.read_bytes() == b"png-bytes"


def test_image_placeholder_combinations_preserve_all_images_and_text(tmp_path):
    conversation_root = tmp_path / "conv"
    first = _image_file(tmp_path, "first.png")
    second = _image_file(tmp_path, "second.jpg")

    cases = [
        (f"[[image:path={first}]]", "[图片：first.png]", ["image"]),
        (
            f"[[image:path={first}]][[image:path={second}]]",
            "[图片：first.png][图片：second.jpg]",
            ["image", "image"],
        ),
        (
            f"[[image:path={first}]] question",
            "[图片：first.png] question",
            ["image", "text"],
        ),
        (
            f"[[image:path={first}]] question [[image:path={second}]]",
            "[图片：first.png] question [图片：second.jpg]",
            ["image", "text", "image"],
        ),
    ]
    for query, expected_display, expected_types in cases:
        display, parts = parse_image_placeholders(query, conversation_root)
        assert display == expected_display
        assert [part["type"] for part in parts] == expected_types


def test_existing_attachment_placeholder_can_be_restored(tmp_path):
    conversation_root = tmp_path / "conv"
    source = _image_file(tmp_path)
    _, parts = parse_image_placeholders(f"[[image:path={source}]]", conversation_root)
    attachment_id = parts[0]["attachment_id"]

    display, restored = parse_image_placeholders(
        f"[[image:id={attachment_id}]]",
        conversation_root,
    )

    assert display == "[图片：sample.png]"
    assert restored[0]["attachment_id"] == attachment_id


def test_finder_style_path_paste_creates_filename_placeholder(tmp_path):
    conversation_root = tmp_path / "conv"
    source = _image_file(tmp_path, "Finder Image.png")

    display, parts = parse_pasted_image_references(str(source), conversation_root)

    assert display == "[图片：Finder Image.png]"
    assert parts[0]["filename"] == "Finder Image.png"
    assert (conversation_root / "attachments" / f"{parts[0]['attachment_id']}_Finder Image.png").is_file()


def test_file_url_paste_creates_filename_placeholder(tmp_path):
    conversation_root = tmp_path / "conv"
    source = _image_file(tmp_path, "url-image.png")

    display, parts = parse_pasted_image_references(source.as_uri(), conversation_root)

    assert display == "[图片：url-image.png]"
    assert parts[0]["filename"] == "url-image.png"


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
    source = _image_file(tmp_path)
    _, parts = parse_image_placeholders(f"question [[image:path={source}]]", conversation_root)
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
    source = _image_file(tmp_path)
    _, parts = parse_image_placeholders(f"question [[image:path={source}]]", conversation_root)
    message = {"role": "user", "content": parts}

    assert sanitize_openai_messages([message])[0]["content"] == [{"type": "text", "text": "question "}]
    assert build_anthropic_request_messages([message])[1][0]["content"] == [{"type": "text", "text": "question "}]
