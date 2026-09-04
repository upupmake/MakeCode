import pytest
from textual.events import Paste

from system.tui_app import MakeCodeTuiApp


@pytest.mark.anyio
async def test_system_image_paste_inserts_text_placeholder_and_deletes_atomically():
    marker = "[[image:id=img_00000000000000000000000000000000]]"
    app = MakeCodeTuiApp(image_clipboard_handler=lambda: marker)

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")

        assert app.paste_image_from_system_clipboard() is True
        assert input_box.text == marker
        assert input_box.cursor_location == input_box.document.end

        await pilot.press("backspace")

        assert input_box.text == ""


@pytest.mark.anyio
async def test_nonempty_finder_paste_prefers_image_bytes_over_filename_text():
    marker = "[[image:id=img_22222222222222222222222222222222]]"
    app = MakeCodeTuiApp(image_clipboard_handler=lambda: marker)

    async with app.run_test(size=(180, 40)) as pilot:
        await pilot.pause()
        input_box = app.query_one("#input-box")
        input_box.on_paste(Paste("img_01_045feb8c.png"))

        assert input_box.text == marker


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

        await pilot.press("ctrl+v")

        assert input_box.text == marker
