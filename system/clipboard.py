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
    path = Path(path_text).expanduser()
    if path.is_symlink() or not path.is_file():
        return None
    expected_type, _ = mimetypes.guess_type(path.name)
    if expected_type not in _SUPPORTED_IMAGE_TYPES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    normalized = _normalize_image_data(data)
    if normalized is None or normalized[1] != expected_type:
        return None
    return normalized[0], path.name, normalized[1]


def _read_text_command(command: list[str]) -> str | None:
    data = _run_binary_command(command)
    if data is None:
        return None
    return data.decode("utf-8", "replace").strip() or None


def _read_file_path_from_macos_clipboard() -> str | None:
    osascript = shutil.which("osascript")
    if not osascript:
        return None
    return _read_text_command([
        osascript,
        "-e",
        "POSIX path of (the clipboard as «class furl»)",
    ])


def _read_file_path_from_windows_clipboard() -> str | None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$files = [System.Windows.Forms.Clipboard]::GetFileDropList(); "
        "if ($files.Count -eq 0) { exit 1 }; "
        "[Console]::Out.Write($files[0])"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return _read_text_command([
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-STA",
        "-EncodedCommand",
        encoded,
    ])


def _file_path_from_uri_list(data: bytes | None) -> str | None:
    if not data:
        return None
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
        return path
    return None


def _read_file_path_from_linux_clipboard() -> str | None:
    wl_paste = shutil.which("wl-paste")
    if wl_paste:
        path = _file_path_from_uri_list(_run_binary_command([
            wl_paste,
            "--no-newline",
            "--type",
            "text/uri-list",
        ]))
        if path:
            return path
    xclip = shutil.which("xclip")
    if xclip:
        return _file_path_from_uri_list(_run_binary_command([
            xclip,
            "-selection",
            "clipboard",
            "-target",
            "text/uri-list",
            "-out",
        ]))
    return None


def read_image_file_from_system_clipboard() -> tuple[bytes, str, str] | None:
    if sys.platform == "darwin":
        path = _read_file_path_from_macos_clipboard()
    elif sys.platform == "win32":
        path = _read_file_path_from_windows_clipboard()
    elif sys.platform.startswith("linux"):
        path = _read_file_path_from_linux_clipboard()
    else:
        path = None
    return _read_image_file(path) if path else None


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


def read_image_from_system_clipboard() -> tuple[bytes, str] | None:
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
