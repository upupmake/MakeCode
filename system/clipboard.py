import shutil
import subprocess
import sys


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
