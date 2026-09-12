import pytest
from textual.events import Paste

from system.tui_app import MakeCodeTuiApp
from utils.vision import IMAGE_PLACEHOLDER_PATTERN


@pytest.mark.anyio
@pytest.mark.parametrize("filename", ["photo.png", "clipboard.png"])
async def test_system_image_paste_displays_filename_and_deletes_atomically(filename):
    marker = "[[image:id=img_00000000000000000000000000000000]]"
    block = {
        "type": "image",
        "attachment_id": "img_00000000000000000000000000000000",
        "filename": filename,
        "media_type": "image/png",
    }
    app = MakeCodeTuiApp(
        image_placeholder_handler=lambda value: (f"[图片：{filename}]", [block]),
        image_clipboard_handler=lambda: marker,
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")

        assert app.paste_image_from_system_clipboard() is True
        assert input_box.text == f"[图片：{filename}]"
        assert app._serialize_input_text(input_box.text) == marker
        assert input_box.cursor_location == input_box.document.end

        await pilot.press("backspace")

        assert input_box.text == ""


@pytest.mark.anyio
@pytest.mark.parametrize("filename", ["photo.png", "clipboard.png"])
async def test_image_display_serializes_to_marker_before_submit(filename):
    marker = "[[image:id=img_66666666666666666666666666666666]]"
    block = {
        "type": "image",
        "attachment_id": "img_66666666666666666666666666666666",
        "filename": filename,
        "media_type": "image/png",
    }
    submitted = []

    async def submit(text):
        submitted.append(text)

    app = MakeCodeTuiApp(
        submit_handler=submit,
        image_placeholder_handler=lambda value: (f"[图片：{filename}]", [block]),
        image_clipboard_handler=lambda: marker,
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        assert app.paste_image_from_system_clipboard() is True
        assert app.query_one("#input-box").text == f"[图片：{filename}]"

        app.submit_current_input()
        for _ in range(20):
            await pilot.pause()
            if submitted:
                break

        assert submitted == [marker]
        assert app._input_history == [marker]


@pytest.mark.anyio
async def test_nonempty_finder_paste_prefers_image_bytes_over_filename_text():
    marker = "[[image:id=img_22222222222222222222222222222222]]"
    block = {
        "type": "image",
        "attachment_id": "img_22222222222222222222222222222222",
        "filename": "photo.png",
        "media_type": "image/png",
    }
    app = MakeCodeTuiApp(
        image_placeholder_handler=lambda value: ("[图片：photo.png]", [block]),
        image_clipboard_handler=lambda: marker,
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.on_paste(Paste("img_01_045feb8c.png"))

        assert input_box.text == "[图片：photo.png]"
        assert app._serialize_input_text(input_box.text) == marker


@pytest.mark.anyio
async def test_ctrl_v_does_not_read_image_clipboard():
    calls = {"count": 0}

    def clipboard_handler():
        calls["count"] += 1
        return "[[image:id=img_77777777777777777777777777777777]]"

    app = MakeCodeTuiApp(image_clipboard_handler=clipboard_handler)

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.focus()

        await pilot.press("ctrl+v")

        assert calls["count"] == 0
        assert input_box.text == ""


@pytest.mark.anyio
async def test_system_image_paste_displays_multiple_filenames():
    first = "[[image:id=img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]]"
    second = "[[image:id=img_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]]"
    blocks = {
        first: {
            "type": "image",
            "attachment_id": "img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "filename": "提示文案.jpg",
            "media_type": "image/jpeg",
        },
        second: {
            "type": "image",
            "attachment_id": "img_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "filename": "没有开关切换.jpg",
            "media_type": "image/jpeg",
        },
    }

    def placeholder_handler(value):
        parts = [blocks[marker] for marker in (first, second) if marker in value]
        display = "".join(f"[图片：{part['filename']}]" for part in parts)
        return display, parts

    app = MakeCodeTuiApp(
        image_placeholder_handler=placeholder_handler,
        image_clipboard_handler=lambda: first + second,
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")

        assert app.paste_image_from_system_clipboard() is True
        assert input_box.text == "[图片：提示文案.jpg][图片：没有开关切换.jpg]"
        assert app._serialize_input_text(input_box.text) == first + second


@pytest.mark.anyio
async def test_split_paste_events_insert_each_clipboard_item_once():
    first = "[[image:id=img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]]"
    second = "[[image:id=img_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]]"
    folder = "/tmp/assets"
    mapping = {
        "提示文案.jpg": first,
        "没有开关切换.jpg": second,
        "assets": folder,
    }
    blocks = {
        first: {
            "type": "image",
            "attachment_id": "img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "filename": "提示文案.jpg",
            "media_type": "image/jpeg",
        },
        second: {
            "type": "image",
            "attachment_id": "img_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "filename": "没有开关切换.jpg",
            "media_type": "image/jpeg",
        },
    }

    def placeholder_handler(value):
        parts = []
        display = []
        position = 0
        for match in IMAGE_PLACEHOLDER_PATTERN.finditer(value):
            if match.start() > position:
                display.append(value[position:match.start()])
            marker = match.group(0)
            part = blocks[marker]
            parts.append(part)
            display.append(f"[图片：{part['filename']}]")
            position = match.end()
        if position < len(value):
            display.append(value[position:])
        return "".join(display), parts

    app = MakeCodeTuiApp(
        image_placeholder_handler=placeholder_handler,
        image_clipboard_handler=lambda paste_text: mapping.get(paste_text or "", None),
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.on_paste(Paste("提示文案.jpg"))
        input_box.on_paste(Paste("没有开关切换.jpg"))
        input_box.on_paste(Paste("assets"))

        assert input_box.text == f"[图片：提示文案.jpg][图片：没有开关切换.jpg]{folder}"
        assert app._serialize_input_text(input_box.text) == first + second + folder


@pytest.mark.anyio
async def test_unmatched_clipboard_filename_is_not_inserted_as_text():
    mapping = {
        "提示文案.jpg": "[[image:id=img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]]",
        "没有开关切换.jpg": "",
    }
    block = {
        "type": "image",
        "attachment_id": "img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "filename": "提示文案.jpg",
        "media_type": "image/jpeg",
    }
    app = MakeCodeTuiApp(
        image_placeholder_handler=lambda value: ("[图片：提示文案.jpg]", [block]),
        image_clipboard_handler=lambda paste_text: mapping.get(paste_text, None),
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.on_paste(Paste("提示文案.jpg"))
        input_box.on_paste(Paste("没有开关切换.jpg"))

        assert input_box.text == "[图片：提示文案.jpg]"


@pytest.mark.anyio
async def test_system_paste_keeps_non_image_paths_between_images():
    first = "[[image:id=img_cccccccccccccccccccccccccccccccc]]"
    second = "[[image:id=img_dddddddddddddddddddddddddddddddd]]"
    notes = "/tmp/notes.txt"
    blocks = {
        first: {
            "type": "image",
            "attachment_id": "img_cccccccccccccccccccccccccccccccc",
            "filename": "photo.png",
            "media_type": "image/png",
        },
        second: {
            "type": "image",
            "attachment_id": "img_dddddddddddddddddddddddddddddddd",
            "filename": "switch.jpg",
            "media_type": "image/jpeg",
        },
    }

    def placeholder_handler(value):
        parts = []
        display = []
        position = 0
        for match in IMAGE_PLACEHOLDER_PATTERN.finditer(value):
            if match.start() > position:
                display.append(value[position:match.start()])
            marker = match.group(0)
            part = blocks[marker]
            parts.append(part)
            display.append(f"[图片：{part['filename']}]")
            position = match.end()
        if position < len(value):
            display.append(value[position:])
        return "".join(display), parts

    app = MakeCodeTuiApp(
        image_placeholder_handler=placeholder_handler,
        image_clipboard_handler=lambda: first + notes + second,
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")

        assert app.paste_image_from_system_clipboard() is True
        assert input_box.text == f"[图片：photo.png]{notes}[图片：switch.jpg]"
        assert app._serialize_input_text(input_box.text) == first + notes + second


@pytest.mark.anyio
async def test_finder_file_path_paste_stays_text(tmp_path):
    source = tmp_path / "Finder Screenshot.png"
    source.write_bytes(b"png clipboard fixture")

    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.on_paste(Paste(str(source)))

        assert input_box.text == str(source)


@pytest.mark.anyio
async def test_normal_text_paste_stays_text():
    app = MakeCodeTuiApp(image_placeholder_handler=lambda text: (text, []))

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.on_paste(Paste("ordinary text"))

        assert input_box.text == "ordinary text"


@pytest.mark.anyio
async def test_multiline_text_paste_stays_text_when_image_clipboard_is_empty():
    text = "curl --url 'https://example.com/api' \\\n" + "  -H '" + ("x" * 300) + "'"
    app = MakeCodeTuiApp(
        image_placeholder_handler=lambda value: (value, []),
        image_clipboard_handler=lambda: None,
    )

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.on_paste(Paste(text))

        assert input_box.text == text


@pytest.mark.anyio
async def test_left_and_right_skip_image_placeholder_atomically():
    marker = "[[image:id=img_33333333333333333333333333333333]]"
    text = f"before {marker} after"
    marker_start = len("before ")
    marker_end = marker_start + len(marker)
    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.load_text(text)
        input_box.focus()

        input_box.cursor_location = (0, marker_start)
        await pilot.press("right")
        assert input_box.cursor_location == (0, marker_end)

        await pilot.press("left")
        assert input_box.cursor_location == (0, marker_start)

        input_box.cursor_location = (0, marker_start + 5)
        await pilot.press("right")
        assert input_box.cursor_location == (0, marker_end)

        input_box.cursor_location = (0, marker_end - 5)
        await pilot.press("left")
        assert input_box.cursor_location == (0, marker_start)


@pytest.mark.anyio
async def test_left_and_right_skip_adjacent_image_placeholders():
    first = "[[image:id=img_44444444444444444444444444444444]]"
    second = "[[image:id=img_55555555555555555555555555555555]]"
    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.load_text(first + second)
        input_box.focus()
        boundary = len(first)

        input_box.cursor_location = (0, boundary)
        await pilot.press("right")
        assert input_box.cursor_location == (0, len(first + second))

        input_box.cursor_location = (0, boundary)
        await pilot.press("left")
        assert input_box.cursor_location == (0, 0)


@pytest.mark.anyio
async def test_left_and_right_keep_normal_text_navigation():
    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.load_text("plain")
        input_box.focus()
        input_box.cursor_location = (0, 2)

        await pilot.press("right")
        assert input_box.cursor_location == (0, 3)

        await pilot.press("left")
        assert input_box.cursor_location == (0, 2)

    marker = "[[image:id=img_11111111111111111111111111111111]]"
    app = MakeCodeTuiApp(image_clipboard_handler=lambda: marker)

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.focus()
        input_box.on_paste(Paste("photo.png"))

        assert input_box.text == marker


@pytest.mark.anyio
@pytest.mark.parametrize(
    "pasted, expected",
    [
        ("https://api.chat.csu.edu.cn/v1\u200b\u200b", "https://api.chat.csu.edu.cn/v1"),
        ("\ufeffhttps://api.example.com/v1\u2060", "https://api.example.com/v1"),
        ("a\u200db", "a\u200db"),
    ],
)
async def test_paste_event_strips_invisible_characters(pasted, expected):
    app = MakeCodeTuiApp()

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.focus()
        await pilot.pause()

        app.post_message(Paste(pasted))
        await pilot.pause()

        assert input_box.text == expected


@pytest.mark.anyio
async def test_paste_event_strips_invisible_characters_before_image_handling():
    marker = "[[image:id=img_88888888888888888888888888888888]]"
    seen: list[str] = []

    def clipboard_handler(paste_text):
        seen.append(paste_text)
        return marker if paste_text == "photo.png" else None

    app = MakeCodeTuiApp(image_clipboard_handler=clipboard_handler)

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.focus()
        await pilot.pause()

        app.post_message(Paste("photo.png\u200b\ufeff"))
        await pilot.pause()

        assert seen == ["photo.png"]
        assert input_box.text == marker
