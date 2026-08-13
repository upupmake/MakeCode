import hashlib
import importlib.metadata
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

os.environ["TS_PACK_OFFLINE"] = "1"

import pyzstd


EXPECTED_PACKAGE_VERSIONS = {
    "tree-sitter": "0.25.2",
    "tree-sitter-language-pack": "1.14.3",
}
EXPECTED_ARCHIVE_SHA256 = {
    "linux-x86_64": "935c0990f08cde9f41ff5519de5129b6b73acebcc80a6db647a1aadf5ca19a77",
    "macos-arm64": "7097f715d07688e6c12740908c712e67d5672aeb05971dec3b65d19cf7080159",
    "windows-x86_64": "03e64093297c3bde2139704af580fc80e34eac1bb08b98f737a325a6f6ee6766",
}


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = "x86_64" if machine in {"amd64", "x86_64"} else "aarch64"
    if sys.platform == "win32":
        return f"windows-{arch}"
    if sys.platform.startswith("linux"):
        return f"linux-{arch}"
    if sys.platform == "darwin" and arch == "aarch64":
        return "macos-arm64"
    raise RuntimeError(f"unsupported platform: {sys.platform}/{platform.machine()}")


def check_parser(cache_dir: Path) -> None:
    for package, expected_version in EXPECTED_PACKAGE_VERSIONS.items():
        actual_version = importlib.metadata.version(package)
        if actual_version != expected_version:
            raise RuntimeError(
                f"{package} version mismatch: {actual_version} != {expected_version}"
            )

    from tree_sitter_language_pack import PackConfig, configure, get_parser

    os.environ["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(cache_dir)
    os.environ["TS_PACK_CACHE_DIR"] = str(cache_dir)
    configure(PackConfig(cache_dir=str(cache_dir)))

    parser = get_parser("python")
    valid_tree = parser.parse(b"def add(a, b):\n    return a + b\n")
    invalid_source = "def add(a, b)\n    return a + b\n"
    invalid_tree = parser.parse(invalid_source.encode())
    print(f"valid_has_error={valid_tree.root_node.has_error}")
    print(f"invalid_has_error={invalid_tree.root_node.has_error}")
    if valid_tree.root_node.has_error or not invalid_tree.root_node.has_error:
        raise RuntimeError("tree-sitter syntax assertions failed")

    from tree_sitter_language_pack import ProcessConfig, process

    result = process(invalid_source, ProcessConfig(
        language="python",
        structure=False,
        imports=False,
        exports=False,
        diagnostics=True,
    ))
    print(f"diagnostics={len(result.diagnostics)}")
    if not result.diagnostics:
        raise RuntimeError("tree-sitter diagnostics assertion failed")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    platform = platform_key()
    archive = root / "ts_cache" / f"parsers-{platform}.tar.zst"
    if not archive.is_file():
        raise FileNotFoundError(archive)
    with archive.open("rb") as archive_file:
        actual_sha256 = hashlib.file_digest(archive_file, "sha256").hexdigest()
    expected_sha256 = EXPECTED_ARCHIVE_SHA256[platform]
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"{archive.name} SHA256 mismatch: {actual_sha256} != {expected_sha256}"
        )

    with tempfile.TemporaryDirectory(prefix="tree-sitter-offline-") as temp_dir:
        cache_dir = Path(temp_dir)
        with pyzstd.open(archive, "rb") as compressed:
            with tarfile.open(fileobj=compressed) as tar:
                tar.extractall(cache_dir, filter="data")

        parser_library_name = {
            "win32": "tree_sitter_python.dll",
            "linux": "libtree_sitter_python.so",
            "darwin": "libtree_sitter_python.dylib",
        }["linux" if sys.platform.startswith("linux") else sys.platform]
        parser_library = cache_dir / parser_library_name
        if not parser_library.is_file():
            raise FileNotFoundError(parser_library)

        print(f"platform={platform}")
        print(f"archive={archive.name}")
        print(f"cache_dir={cache_dir}")
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--check", str(cache_dir)],
            check=True,
            timeout=15,
            env={**os.environ, "TS_PACK_OFFLINE": "1"},
        )

    print("offline tree-sitter verification passed")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        check_parser(Path(sys.argv[2]))
    else:
        main()
