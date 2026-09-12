import base64
import mimetypes
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from urllib.parse import unquote, urlparse


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TIFF_SIGNATURES = (b"II*\x00", b"MM\x00*")
_SUPPORTED_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}
# 不含 U+200C/U+200D：ZWJ/ZWNJ 是 emoji 组合与阿拉伯、印度语系连写的必需字符。
_INVISIBLE_CHARACTERS = str.maketrans("", "", "\u200b\ufeff\u2060")


def strip_invisible_characters(text: str) -> str:
    """剥离粘带进来的零宽字符，避免 URL、密钥等被不可见字符污染。"""
    return text.translate(_INVISIBLE_CHARACTERS)


def _valid_png(data: bytes) -> bool:
    if not data.startswith(_PNG_SIGNATURE):
        return False
    position = len(_PNG_SIGNATURE)
    has_header = False
    has_data = False
    has_end = False
    compressed = bytearray()
    while position + 12 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        chunk_end = position + 12 + length
        if chunk_end > len(data):
            return False
        chunk_type = data[position + 4:position + 8]
        chunk_data = data[position + 8:position + 8 + length]
        chunk_crc = struct.unpack(">I", data[position + 8 + length:chunk_end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xffffffff != chunk_crc:
            return False
        if chunk_type == b"IHDR":
            if has_header or length != 13:
                return False
            width, height = struct.unpack(">II", chunk_data[:8])
            if width == 0 or height == 0:
                return False
            has_header = True
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
            has_data = True
        elif chunk_type == b"IEND":
            has_end = True
            position = chunk_end
            break
        position = chunk_end
    if not (has_header and has_data and has_end):
        return False
    try:
        zlib.decompress(bytes(compressed))
    except zlib.error:
        return False
    return position == len(data)


def _image_format(data: bytes) -> str | None:
    if _valid_png(data):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 12:
        return "image/webp"
    return None


def _apple_script_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _read_clipboard_format_with_osascript(output_path: Path, clipboard_class: str) -> bool:
    osascript = shutil.which("osascript")
    if not osascript:
        return False
    path = _apple_script_path(output_path)
    script = f'''set outputPath to "{path}"
try
    set imageData to the clipboard as {clipboard_class}
    set outputFile to open for access POSIX file outputPath with write permission
    set eof outputFile to 0
    write imageData to outputFile
    close access outputFile
    return "ok"
on error
    try
        close access POSIX file outputPath
    end try
    return "no"
end try'''
    try:
        result = subprocess.run(
            [osascript, "-e", script],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0


def _run_binary_command(command: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def _normalize_image_data(data: bytes) -> tuple[bytes, str] | None:
    media_type = _image_format(data)
    if media_type in _SUPPORTED_IMAGE_TYPES:
        return data, media_type
    if data.startswith(_TIFF_SIGNATURES):
        converted = _png_from_tiff(data)
        if converted:
            return converted, "image/png"
    return None


def _read_image_with_osascript() -> tuple[bytes, str] | None:
    if shutil.which("osascript") is None:
        return None
    with tempfile.TemporaryDirectory(prefix="makecode-clipboard-") as directory:
        directory_path = Path(directory)
        png_path = directory_path / "clipboard.png"
        if _read_clipboard_format_with_osascript(png_path, "«class PNGf»"):
            normalized = _normalize_image_data(png_path.read_bytes())
            if normalized:
                return normalized

        tiff_path = directory_path / "clipboard.tiff"
        if _read_clipboard_format_with_osascript(tiff_path, "«class TIFF»"):
            normalized = _normalize_image_data(tiff_path.read_bytes())
            if normalized:
                return normalized
    return None


def _png_from_tiff(data: bytes) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="makecode-clipboard-") as directory:
        source = Path(directory) / "clipboard.tiff"
        target = Path(directory) / "clipboard.png"
        source.write_bytes(data)
        if sys.platform == "darwin":
            converter = shutil.which("sips")
            command = (
                [converter, "-s", "format", "png", str(source), "--out", str(target)]
                if converter else None
            )
        else:
            magick = shutil.which("magick")
            convert = shutil.which("convert")
            command = [magick, str(source), str(target)] if magick else (
                [convert, str(source), str(target)] if convert else None
            )
        if command is None:
            return None
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        converted = target.read_bytes() if target.is_file() else b""
        return converted if converted.startswith(_PNG_SIGNATURE) else None


def _read_image_file(path_text: str) -> tuple[bytes, str, str] | None:
    try:
        path = Path(path_text).expanduser()
        if path.is_symlink() or not path.is_file():
            return None
    except OSError:
        return None
    expected_type, _ = mimetypes.guess_type(path.name)
    if expected_type not in _SUPPORTED_IMAGE_TYPES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    normalized = _normalize_image_data(data)
    if normalized is None:
        return None
    image_bytes, media_type = normalized
    if media_type == expected_type:
        return image_bytes, path.name, media_type
    # 扩展名与实际内容不符时以内容为准，改写扩展名保证下游按扩展名推导类型的一致性
    extension = "jpg" if media_type == "image/jpeg" else media_type.removeprefix("image/")
    return image_bytes, f"{path.stem}.{extension}", media_type


def _read_text_command(command: list[str]) -> str | None:
    data = _run_binary_command(command)
    if data is None:
        return None
    return data.decode("utf-8", "replace").strip() or None


def _split_clipboard_path_payload(payload: str | None) -> list[str]:
    if not payload:
        return []
    paths = []
    seen = set()
    for line in payload.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        path = line.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _read_file_paths_from_macos_clipboard() -> list[str]:
    osascript = shutil.which("osascript")
    if not osascript:
        return []
    script = '''
use framework "AppKit"
set thePasteboard to current application's NSPasteboard's generalPasteboard()
set theFiles to (thePasteboard's propertyListForType:(current application's NSFilenamesPboardType))
if theFiles is missing value then return ""
set AppleScript's text item delimiters to linefeed
return (theFiles as list) as text
'''
    paths = _split_clipboard_path_payload(_read_text_command([osascript, "-e", script]))
    if paths:
        return paths
    return _split_clipboard_path_payload(_read_text_command([
        osascript,
        "-e",
        "POSIX path of (the clipboard as «class furl»)",
    ]))


def _read_file_path_from_macos_clipboard() -> str | None:
    paths = _read_file_paths_from_macos_clipboard()
    return paths[0] if paths else None


def _read_file_path_from_windows_clipboard() -> str | None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$files = [System.Windows.Forms.Clipboard]::GetFileDropList(); "
        "if ($files.Count -eq 0) { exit 1 }; "
        "[Console]::Out.Write(($files | ForEach-Object { $_ }) -join [Environment]::NewLine)"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    path_data = _run_binary_command([
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-STA",
        "-EncodedCommand",
        encoded,
    ])
    if not path_data:
        return None
    try:
        raw_payload = path_data.decode("utf-8", "replace").strip()
    except UnicodeDecodeError:
        raw_payload = ""
    try:
        legacy_payload = base64.b64decode(path_data.strip(), validate=True).decode("utf-16le")
    except (ValueError, UnicodeDecodeError):
        legacy_payload = ""
    raw_paths = _split_clipboard_path_payload(raw_payload)
    if any(Path(path).is_file() for path in _split_clipboard_path_payload(legacy_payload)):
        return "\n".join(_split_clipboard_path_payload(legacy_payload))
    return "\n".join(raw_paths) if raw_paths else None


def _file_path_from_uri_list(data: bytes | None) -> str | None:
    paths = _file_paths_from_uri_list(data)
    return paths[0] if paths else None


def _file_paths_from_uri_list(data: bytes | None) -> list[str]:
    if not data:
        return []
    paths = []
    seen = set()
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        if parsed.scheme != "file":
            continue
        path = unquote(parsed.path)
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        if sys.platform == "win32" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _read_file_path_from_linux_clipboard() -> str | None:
    wl_paste = shutil.which("wl-paste")
    if wl_paste:
        path = _file_paths_from_uri_list(_run_binary_command([
            wl_paste,
            "--no-newline",
            "--type",
            "text/uri-list",
        ]))
        if path:
            return "\n".join(path)
    xclip = shutil.which("xclip")
    if xclip:
        path = _file_paths_from_uri_list(_run_binary_command([
            xclip,
            "-selection",
            "clipboard",
            "-target",
            "text/uri-list",
            "-out",
        ]))
        if path:
            return "\n".join(path)
    return None


def _read_system_clipboard_file_path() -> str | None:
    paths = _read_system_clipboard_file_paths()
    return paths[0] if paths else None


def _read_system_clipboard_file_paths() -> list[str]:
    if sys.platform == "darwin":
        return _read_file_paths_from_macos_clipboard()
    if sys.platform == "win32":
        return _split_clipboard_path_payload(_read_file_path_from_windows_clipboard())
    if sys.platform.startswith("linux"):
        return _split_clipboard_path_payload(_read_file_path_from_linux_clipboard())
    return []


def read_image_file_from_system_clipboard() -> tuple[bytes, str, str] | None:
    images = read_image_files_from_system_clipboard()
    return images[0] if images else None


def read_image_files_from_system_clipboard() -> list[tuple[bytes, str, str]]:
    return [
        item["image"]
        for item in read_clipboard_file_items()
        if item["kind"] == "image"
    ]


def _clipboard_item_from_path(path_text: str) -> dict[str, object] | None:
    image = _read_image_file(path_text)
    if image is not None:
        return {"kind": "image", "image": image}
    try:
        path = Path(path_text).expanduser()
        if path.is_symlink() or not path.exists():
            return None
    except OSError:
        return None
    return {"kind": "path", "path": str(path)}


def _path_name_candidates(value: str) -> list[str]:
    raw = value.strip().strip("\"'")
    if not raw:
        return []
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        raw = path or raw
    normalized = raw.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    candidates = [raw, normalized, name, stem]
    names = []
    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            names.append(candidate)
    return names


def _clipboard_item_names(item: dict[str, object]) -> set[str]:
    names = set()
    if item.get("kind") == "image":
        image = item.get("image")
        if isinstance(image, tuple) and len(image) >= 2 and isinstance(image[1], str):
            names.update(_path_name_candidates(image[1]))
    path = item.get("path")
    if isinstance(path, str) and path:
        names.update(_path_name_candidates(path))
    return names


def _paste_text_names(paste_text: str | None) -> list[str]:
    if not paste_text:
        return []
    names = []
    seen = set()
    for line in paste_text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        for name in _path_name_candidates(line):
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def read_clipboard_file_items(paste_text: str | None = None) -> list[dict[str, object]]:
    items = []
    for path_text in _read_system_clipboard_file_paths():
        item = _clipboard_item_from_path(path_text)
        if item is not None:
            items.append(item)
    names = _paste_text_names(paste_text)
    if not names:
        return items
    matched = []
    remaining = list(items)
    for name in names:
        for index, item in enumerate(remaining):
            if name in _clipboard_item_names(item):
                matched.append(item)
                del remaining[index]
                break
    if _is_truncated_file_list_paste(paste_text, matched, items):
        return items
    return matched


def _is_truncated_file_list_paste(
    paste_text: str,
    matched: list[dict[str, object]],
    items: list[dict[str, object]],
) -> bool:
    # Windows Terminal 把多文件剪贴板转成粘贴文本时只保留第一个路径。
    # macOS/Linux 终端会按文件拆成多次 Paste，第一次匹配第一项不能展开成整组。
    if sys.platform != "win32":
        return False
    if len(matched) != 1 or len(items) <= 1:
        return False
    if matched[0] is not items[0]:
        return False
    return "\\" in paste_text or "/" in paste_text


def clipboard_paste_text_matches_file_items(paste_text: str | None) -> bool:
    names = set(_paste_text_names(paste_text))
    if not names:
        return False
    for item in read_clipboard_file_items():
        if names & _clipboard_item_names(item):
            return True
    return False


def _read_image_from_macos_clipboard() -> tuple[bytes, str] | None:
    pbpaste = shutil.which("pbpaste")
    if pbpaste:
        data = _run_binary_command([pbpaste, "-Prefer", "tiff"])
        if data:
            normalized = _normalize_image_data(data)
            if normalized:
                return normalized
    return _read_image_with_osascript()


def _read_image_from_windows_clipboard() -> tuple[bytes, str] | None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    with tempfile.TemporaryDirectory(prefix="makecode-clipboard-") as directory:
        output_path = Path(directory) / "clipboard.png"
        escaped_path = str(output_path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$image = [System.Windows.Forms.Clipboard]::GetImage(); "
            "if ($null -eq $image) { exit 1 }; "
            f"$image.Save('{escaped_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
            "$image.Dispose()"
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        data = output_path.read_bytes() if output_path.is_file() else b""
        return _normalize_image_data(data)


def _read_image_from_linux_clipboard() -> tuple[bytes, str] | None:
    image_types = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/tiff")
    wl_paste = shutil.which("wl-paste")
    if wl_paste:
        for media_type in image_types:
            data = _run_binary_command([wl_paste, "--no-newline", "--type", media_type])
            if data:
                normalized = _normalize_image_data(data)
                if normalized:
                    return normalized
    xclip = shutil.which("xclip")
    if xclip:
        for media_type in image_types:
            data = _run_binary_command([xclip, "-selection", "clipboard", "-target", media_type, "-out"])
            if data:
                normalized = _normalize_image_data(data)
                if normalized:
                    return normalized
    return None


def _clipboard_has_existing_file_source() -> bool:
    for path_text in _read_system_clipboard_file_paths():
        try:
            if Path(path_text).expanduser().is_file():
                return True
        except OSError:
            continue
    return False


def read_image_from_system_clipboard(
    *,
    skip_if_file_source: bool = False,
) -> tuple[bytes, str] | None:
    if skip_if_file_source and _clipboard_has_existing_file_source():
        return None
    if sys.platform == "darwin":
        return _read_image_from_macos_clipboard()
    if sys.platform == "win32":
        return _read_image_from_windows_clipboard()
    if sys.platform.startswith("linux"):
        return _read_image_from_linux_clipboard()
    return None


def copy_to_system_clipboard(text: str) -> bool:
    if sys.platform == "darwin":
        commands = [[shutil.which("pbcopy")]]
        encoding = "utf-8"
    elif sys.platform == "win32":
        commands = [[shutil.which("clip")]]
        encoding = "utf-16le"
    elif sys.platform.startswith("linux"):
        encoding = "utf-8"
        commands = []
        wl_copy = shutil.which("wl-copy")
        if wl_copy:
            commands.append([wl_copy])
        xclip = shutil.which("xclip")
        if xclip:
            commands.append([xclip, "-selection", "clipboard"])
        xsel = shutil.which("xsel")
        if xsel:
            commands.append([xsel, "--clipboard", "--input"])
    else:
        return False

    for command in commands:
        if not command[0]:
            continue
        try:
            subprocess.run(
                command,
                input=text.encode(encoding),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        return True
    return False
