import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pyzstd

MAX_GLIBC = (2, 34)
GLIBC_PATTERN = re.compile(r"GLIBC_(\d+)\.(\d+)")


def elf_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as file:
                if file.read(4) == b"\x7fELF":
                    yield path


def check_file(path: Path) -> tuple[int, int] | None:
    result = subprocess.run(
        ["objdump", "-T", str(path)],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    versions = [tuple(map(int, match)) for match in GLIBC_PATTERN.findall(result.stdout)]
    highest = max(versions, default=None)
    if highest and highest > MAX_GLIBC:
        raise RuntimeError(f"{path} requires GLIBC_{highest[0]}.{highest[1]}")
    return highest


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    archive = root / "ts_cache" / "parsers-linux-x86_64.tar.zst"
    if not archive.is_file():
        raise FileNotFoundError(archive)

    checked = 0
    with tempfile.TemporaryDirectory(prefix="tree-sitter-glibc-") as temp_dir:
        parser_dir = Path(temp_dir)
        with pyzstd.open(archive, "rb") as compressed:
            with tarfile.open(fileobj=compressed) as tar:
                tar.extractall(parser_dir, filter="data")

        for scan_root in (root / "build" / "MakeCode", root / "dist", parser_dir):
            for path in elf_files(scan_root):
                highest = check_file(path)
                if highest:
                    print(f"{path}: GLIBC_{highest[0]}.{highest[1]}")
                checked += 1

    if not checked:
        raise RuntimeError("no ELF files found")
    print(f"verified {checked} ELF files require at most GLIBC_2.34")


if __name__ == "__main__":
    if sys.platform != "linux":
        raise SystemExit("Linux only")
    main()
