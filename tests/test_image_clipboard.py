import random
import struct
import subprocess
import sys
import zlib
from unittest.mock import patch

import pytest

import main
from system.clipboard import (
    read_image_file_from_system_clipboard,
    read_image_from_system_clipboard,
)
from utils.conversations import ConversationStore
from utils.vision import store_image_bytes_attachment


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    )


def _random_png(seed: int) -> tuple[bytes, bytes, int, int]:
    width, height = 9, 7
    rng = random.Random(seed)
    random_bytes = bytes(rng.randrange(256) for _ in range(width * height * 3))
    scanlines = b"".join(
        b"\x00" + random_bytes[row * width * 3:(row + 1) * width * 3]
        for row in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    image = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )
    return image, scanlines, width, height


def _png_scanlines(data: bytes) -> tuple[int, int, bytes]:
    position = 8
    compressed = bytearray()
    width = height = 0
    while position < len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        chunk = data[position + 8:position + 8 + length]
        position += length + 12
        if kind == b"IHDR":
            width, height = struct.unpack(">II", chunk[:8])
        elif kind == b"IDAT":
            compressed.extend(chunk)
        elif kind == b"IEND":
            break
    return width, height, zlib.decompress(bytes(compressed))


def _assert_image_content(data: bytes, expected_scanlines: bytes, width: int, height: int) -> None:
    actual_width, actual_height, actual_scanlines = _png_scanlines(data)
    assert (actual_width, actual_height) == (width, height)
    assert actual_scanlines == expected_scanlines



def test_read_image_from_system_clipboard_reads_png_bytes():
    png, scanlines, width, height = _random_png(1)
    with (
        patch("system.clipboard.sys.platform", "darwin"),
        patch("system.clipboard.shutil.which", return_value="/usr/bin/pbpaste"),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["/usr/bin/pbpaste", "-Prefer", "tiff"], 0, stdout=png,
        )) as run,
    ):
        assert read_image_from_system_clipboard() == (png, "image/png")

    run.assert_called_once_with(
        ["/usr/bin/pbpaste", "-Prefer", "tiff"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    _assert_image_content(png, scanlines, width, height)


def test_read_image_from_system_clipboard_ignores_text_clipboard():
    with (
        patch("system.clipboard.sys.platform", "darwin"),
        patch("system.clipboard.shutil.which", return_value="/usr/bin/pbpaste"),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["/usr/bin/pbpaste", "-Prefer", "tiff"], 0, stdout=b"plain text",
        )),
    ):
        assert read_image_from_system_clipboard() is None


def test_read_image_from_macos_file_clipboard_returns_original_bytes(tmp_path):
    image, scanlines, width, height = _random_png(10)
    source = tmp_path / "original.png"
    source.write_bytes(image)

    with (
        patch("system.clipboard.sys.platform", "darwin"),
        patch("system.clipboard.shutil.which", return_value="/usr/bin/osascript"),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["/usr/bin/osascript"], 0, stdout=(str(source) + "\n").encode(),
        )),
    ):
        result = read_image_file_from_system_clipboard()

    assert result == (image, "original.png", "image/png")
    _assert_image_content(result[0], scanlines, width, height)


def test_read_image_from_linux_wayland_clipboard():
    png, scanlines, width, height = _random_png(2)

    def which(command):
        return "/usr/bin/wl-paste" if command == "wl-paste" else None

    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=which),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["/usr/bin/wl-paste"], 0, stdout=png,
        )) as run,
    ):
        assert read_image_from_system_clipboard() == (png, "image/png")

    run.assert_called_once_with(
        ["/usr/bin/wl-paste", "--no-newline", "--type", "image/png"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    _assert_image_content(png, scanlines, width, height)


def test_read_image_file_from_windows_file_clipboard(tmp_path):
    image, scanlines, width, height = _random_png(11)
    source = tmp_path / "windows.png"
    source.write_bytes(image)

    with (
        patch("system.clipboard.sys.platform", "win32"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "powershell.exe" if command == "powershell.exe" else None),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["powershell.exe"], 0, stdout=(str(source) + "\n").encode(),
        )),
    ):
        result = read_image_file_from_system_clipboard()

    assert result == (image, "windows.png", "image/png")
    _assert_image_content(result[0], scanlines, width, height)


def test_windows_file_clipboard_script_forces_utf8_output(tmp_path):
    image, _, _, _ = _random_png(13)
    source = tmp_path / "壁纸.png"
    source.write_bytes(image)

    with (
        patch("system.clipboard.sys.platform", "win32"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "powershell.exe" if command == "powershell.exe" else None),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["powershell.exe"], 0, stdout=(str(source) + "\n").encode("utf-8"),
        )) as run,
    ):
        assert read_image_file_from_system_clipboard() == (image, "壁纸.png", "image/png")

    encoded = run.call_args.args[0][run.call_args.args[0].index("-EncodedCommand") + 1]
    import base64
    script = base64.b64decode(encoded).decode("utf-16le")
    assert script.startswith("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;")


def test_read_image_file_accepts_mismatched_extension_using_content_type(tmp_path):
    # 扩展名是 .png 但内容是 JPEG（常见于壁纸/下载图片）：以内容类型为准并改写扩展名
    jpeg = b"\xff\xd8\xff" + b"jpeg-body" + b"\xff\xd9"
    source = tmp_path / "【在室内】2024-08-17 02_34_00.png"
    source.write_bytes(jpeg)

    with (
        patch("system.clipboard.sys.platform", "win32"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "powershell.exe" if command == "powershell.exe" else None),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["powershell.exe"], 0, stdout=(str(source) + "\n").encode("utf-8"),
        )),
    ):
        result = read_image_file_from_system_clipboard()

    assert result == (jpeg, "【在室内】2024-08-17 02_34_00.jpg", "image/jpeg")


@pytest.mark.skipif(sys.platform == "win32", reason="Linux file URI route requires a POSIX host")
def test_read_image_file_from_linux_uri_clipboard(tmp_path):
    image, scanlines, width, height = _random_png(12)
    source = tmp_path / "linux.png"
    source.write_bytes(image)

    def which(command):
        return "/usr/bin/wl-paste" if command == "wl-paste" else None

    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=which),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["/usr/bin/wl-paste"], 0, stdout=(source.as_uri() + "\n").encode(),
        )),
    ):
        result = read_image_file_from_system_clipboard()

    assert result == (image, "linux.png", "image/png")
    _assert_image_content(result[0], scanlines, width, height)


def test_read_image_from_windows_clipboard_exports_png(tmp_path):
    png, scanlines, width, height = _random_png(3)

    def run(command, **kwargs):
        (tmp_path / "clipboard.png").write_bytes(png)
        return subprocess.CompletedProcess(command, 0)

    class TemporaryDirectory:
        def __enter__(self):
            return str(tmp_path)

        def __exit__(self, *args):
            return False

    with (
        patch("system.clipboard.sys.platform", "win32"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "powershell.exe" if command == "powershell.exe" else None),
        patch("system.clipboard.tempfile.TemporaryDirectory", return_value=TemporaryDirectory()),
        patch("system.clipboard.subprocess.run", side_effect=run) as run_mock,
    ):
        assert read_image_from_system_clipboard() == (png, "image/png")

    command = run_mock.call_args.args[0]
    assert command[0] == "powershell.exe"
    assert "-STA" in command
    _assert_image_content(png, scanlines, width, height)


def test_all_platform_file_clipboards_preserve_generated_image_content(tmp_path):
    image, scanlines, width, height = _random_png(20)
    source = tmp_path / "copied-image.png"
    source.write_bytes(image)
    clipboard_payloads = {
        "darwin": str(source).encode(),
        "win32": str(source).encode(),
        "linux": (source.as_uri() + "\n").encode(),
    }
    if sys.platform == "win32":
        # Linux file URI 无法映射到 Windows 宿主的真实路径，跳过 linux 分支
        del clipboard_payloads["linux"]

    for platform, payload in clipboard_payloads.items():
        if platform == "darwin":
            which = lambda command: "/usr/bin/osascript" if command == "osascript" else None
        elif platform == "win32":
            which = lambda command: "powershell.exe" if command == "powershell.exe" else None
        else:
            which = lambda command: "/usr/bin/wl-paste" if command == "wl-paste" else None
        with (
            patch("system.clipboard.sys.platform", platform),
            patch("system.clipboard.shutil.which", side_effect=which),
            patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
                ["clipboard-provider"], 0, stdout=payload,
            )),
        ):
            result = read_image_file_from_system_clipboard()

        assert result == (image, "copied-image.png", "image/png")
        _assert_image_content(result[0], scanlines, width, height)


def test_all_platform_screenshot_clipboards_preserve_in_memory_image_content(tmp_path):
    screenshot, scanlines, width, height = _random_png(21)

    with (
        patch("system.clipboard.sys.platform", "darwin"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "/usr/bin/pbpaste" if command == "pbpaste" else None),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["pbpaste"], 0, stdout=screenshot,
        )),
    ):
        mac_result = read_image_from_system_clipboard()
    assert mac_result == (screenshot, "image/png")
    _assert_image_content(mac_result[0], scanlines, width, height)

    class TemporaryDirectory:
        def __enter__(self):
            return str(tmp_path)

        def __exit__(self, *args):
            return False

    def windows_run(command, **kwargs):
        (tmp_path / "clipboard.png").write_bytes(screenshot)
        return subprocess.CompletedProcess(command, 0)

    with (
        patch("system.clipboard.sys.platform", "win32"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "powershell.exe" if command == "powershell.exe" else None),
        patch("system.clipboard.tempfile.TemporaryDirectory", return_value=TemporaryDirectory()),
        patch("system.clipboard.subprocess.run", side_effect=windows_run),
    ):
        windows_result = read_image_from_system_clipboard()
    assert windows_result == (screenshot, "image/png")
    _assert_image_content(windows_result[0], scanlines, width, height)

    for command_name in ("wl-paste", "xclip"):
        def which(command, command_name=command_name):
            return f"/usr/bin/{command_name}" if command == command_name else None

        with (
            patch("system.clipboard.sys.platform", "linux"),
            patch("system.clipboard.shutil.which", side_effect=which),
            patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
                [command_name], 0, stdout=screenshot,
            )),
        ):
            linux_result = read_image_from_system_clipboard()

        assert linux_result == (screenshot, "image/png")
        _assert_image_content(linux_result[0], scanlines, width, height)


def test_clipboard_file_routes_reject_non_image_files(tmp_path):
    source = tmp_path / "not-an-image.txt"
    source.write_text("not an image", encoding="utf-8")

    for platform in ("darwin", "win32", "linux"):
        if platform == "linux":
            payload = (source.as_uri() + "\n").encode()
            which = lambda command: "/usr/bin/wl-paste" if command == "wl-paste" else None
        else:
            payload = (str(source) + "\n").encode()
            executable = "osascript" if platform == "darwin" else "powershell.exe"
            which = lambda command, executable=executable: executable if command == executable else None
        with (
            patch("system.clipboard.sys.platform", platform),
            patch("system.clipboard.shutil.which", side_effect=which),
            patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
                ["clipboard-provider"], 0, stdout=payload,
            )),
        ):
            assert read_image_file_from_system_clipboard() is None


def test_clipboard_text_is_not_an_image():
    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "/usr/bin/wl-paste" if command == "wl-paste" else None),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["wl-paste"], 0, stdout=b"ordinary text",
        )),
    ):
        assert read_image_from_system_clipboard() is None



def test_main_clipboard_callback_stores_original_filename_and_bytes(tmp_path):
    image, scanlines, width, height = _random_png(30)
    source = tmp_path / "original.png"
    source.write_bytes(image)
    store = ConversationStore(tmp_path / "conversations")

    with (
        patch.object(main, "CONVERSATION_STORE", store),
        patch.object(main, "read_image_file_from_system_clipboard", return_value=(
            image,
            source.name,
            "image/png",
        )),
        patch.object(main, "read_image_from_system_clipboard", return_value=None),
    ):
        marker = main._paste_image_from_system_clipboard()

    assert marker.startswith("[[image:id=img_")
    attachment = next((store.active_root / "attachments").iterdir())
    assert attachment.name.endswith("_original.png")
    assert attachment.read_bytes() == image
    _assert_image_content(attachment.read_bytes(), scanlines, width, height)


def test_main_clipboard_callback_stores_screenshot_without_file_source(tmp_path):
    image, scanlines, width, height = _random_png(31)
    store = ConversationStore(tmp_path / "conversations")

    with (
        patch.object(main, "CONVERSATION_STORE", store),
        patch.object(main, "read_image_file_from_system_clipboard", return_value=None),
        patch.object(main, "read_image_from_system_clipboard", return_value=(image, "image/png")),
    ):
        marker = main._paste_image_from_system_clipboard()

    assert marker.startswith("[[image:id=img_")
    attachment = next((store.active_root / "attachments").iterdir())
    assert attachment.name.endswith("_clipboard.png")
    assert attachment.read_bytes() == image
    _assert_image_content(attachment.read_bytes(), scanlines, width, height)


def test_invalid_png_bytes_are_not_treated_as_screenshot():
    invalid_png = b"\x89PNG\r\n\x1a\nnot-an-image"

    with (
        patch("system.clipboard.sys.platform", "linux"),
        patch("system.clipboard.shutil.which", side_effect=lambda command: "/usr/bin/wl-paste" if command == "wl-paste" else None),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["wl-paste"], 0, stdout=invalid_png,
        )),
    ):
        assert read_image_from_system_clipboard() is None


def test_image_named_file_with_non_image_content_is_rejected(tmp_path):
    source = tmp_path / "fake.png"
    source.write_text("this is not a PNG", encoding="utf-8")

    with (
        patch("system.clipboard.sys.platform", "darwin"),
        patch("system.clipboard.shutil.which", return_value="/usr/bin/osascript"),
        patch("system.clipboard.subprocess.run", return_value=subprocess.CompletedProcess(
            ["osascript"], 0, stdout=(str(source) + "\n").encode(),
        )),
    ):
        assert read_image_file_from_system_clipboard() is None


def test_store_image_bytes_attachment_persists_clipboard_data(tmp_path):
    block = store_image_bytes_attachment(
        tmp_path / "conversation",
        b"clipboard-png",
        "clipboard.png",
        "image/png",
    )

    path = tmp_path / "conversation" / "attachments" / f"{block['attachment_id']}_clipboard.png"
    assert path.read_bytes() == b"clipboard-png"
    assert block["filename"] == "clipboard.png"
    assert block["media_type"] == "image/png"
