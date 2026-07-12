"""
发布脚本 - 自动生成 version.json 并准备上传文件。
用法: python release.py --release_log <发布日志文件路径>
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from version import CURRENT_VERSION, UPDATE_SERVER_URL


def get_sha256(file_path: Path) -> str:
    """计算文件的 SHA256。"""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="生成 version.json 发布文件")
    parser.add_argument("--release_log", required=True, type=Path, help="发布日志文件路径（markdown 格式）")
    args = parser.parse_args()

    log_path: Path = args.release_log
    if not log_path.exists():
        print(f"❌ 找不到发布日志文件 {log_path}")
        sys.exit(1)
    release_log = log_path.read_text(encoding="utf-8").strip()

    app_dir = Path("dist") / "MakeCode"
    exe_path = app_dir / "MakeCode.exe"
    if not exe_path.exists():
        print(f"❌ 找不到 {exe_path}，请先运行 pyinstaller MakeCode.spec")
        sys.exit(1)

    archive = Path("dist") / "MakeCode-Windows-X64.zip"
    archive.unlink(missing_ok=True)
    shutil.make_archive(str(archive.with_suffix("")), "zip", app_dir.parent, app_dir.name)
    sha256 = get_sha256(archive)
    version_info = {
        "version": CURRENT_VERSION,
        "download_url": f"{UPDATE_SERVER_URL}/{archive.name}",
        "sha256": sha256,
        "size": archive.stat().st_size,
        "release_log": release_log,
    }

    output = Path("dist") / "version.json"
    output.write_text(json.dumps(version_info, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[OK] 已生成 {output}")
    print(f"   版本: {CURRENT_VERSION}")
    print(f"   发布日志: {release_log}")
    print(f"   SHA256: {sha256}")
    print()
    print("请将 dist 目录下的文件上传到服务器:")
    print(f"   1. {archive.name}  ->  {UPDATE_SERVER_URL}/{archive.name}")
    print(f"   2. version.json  ->  {UPDATE_SERVER_URL}/version.json")


if __name__ == "__main__":
    main()
