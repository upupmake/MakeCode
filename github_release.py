"""
GitHub Release 创建与收尾脚本。
用法:
    python github_release.py
    python github_release.py --finalize-release v6.1.8

需要设置环境变量 GITHUB_TOKEN，或在同目录下创建 .github_token 文件。
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from version import CURRENT_VERSION

# GitHub 配置
GITHUB_OWNER = "upupmake"
GITHUB_REPO = "MakeCode"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
REQUIRED_RELEASE_ASSETS = {
    "MakeCode-Windows-X64.zip",
    "MakeCode-macOS-ARM64.zip",
    "MakeCode-Linux-X64.zip",
    "version.json",
}


def get_token() -> str:
    """获取 GitHub Token，优先从环境变量读取，其次从文件读取。"""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token

    token_file = Path(__file__).parent / ".github_token"
    if token_file.exists():
        return token_file.read_text().strip()

    print("❌ 未找到 GitHub Token")
    print("   请设置环境变量 GITHUB_TOKEN，或创建 .github_token 文件")
    sys.exit(1)


def github_request(
    token: str,
    method: str,
    path: str,
    payload: dict | None = None,
):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status == 204:
            return None
        return json.load(response)


def get_all_releases(token: str) -> list:
    """获取所有 Releases。"""
    return github_request(token, "GET", "/releases?per_page=100")


def delete_release(token: str, release_id: int):
    """删除指定 Release。"""
    github_request(token, "DELETE", f"/releases/{release_id}")


def delete_tag(token: str, tag: str):
    """删除指定 tag。"""
    github_request(token, "DELETE", f"/git/refs/tags/{tag}")


def mark_release_as_latest(token: str, release_id: int):
    """将资产齐全的 Release 设为 latest。"""
    github_request(
        token,
        "PATCH",
        f"/releases/{release_id}",
        {"make_latest": "true"},
    )


def release_tag_matches_minor(tag: str, major_version: str, minor_version: str) -> bool:
    """判断 tag 是否属于指定次版本线。"""
    version = tag[1:] if tag.startswith("v") else tag
    parts = version.split(".")
    return (
        len(parts) == 3
        and all(part.isdigit() for part in parts)
        and parts[:2] == [major_version, minor_version]
    )


def finalize_release(token: str, current_tag: str):
    """新版本资产齐全后设为 latest，并清理同一次版本线内的旧版本。"""
    version = current_tag[1:] if current_tag.startswith("v") else current_tag
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"无效的发布 tag: {current_tag}")
    major_version, minor_version, current_patch = parts

    releases = get_all_releases(token)
    current_release = next(
        (release for release in releases if release["tag_name"] == current_tag),
        None,
    )
    if current_release is None:
        raise RuntimeError(f"找不到当前 Release: {current_tag}")
    uploaded_assets = {
        asset.get("name")
        for asset in current_release.get("assets", [])
        if isinstance(asset, dict)
    }
    missing_assets = REQUIRED_RELEASE_ASSETS - uploaded_assets
    if missing_assets:
        raise RuntimeError(
            f"{current_tag} 发布资产尚未齐全，停止清理: "
            f"{', '.join(sorted(missing_assets))}"
        )

    matching_releases = []
    for release in releases:
        tag = release["tag_name"]
        if not release_tag_matches_minor(tag, major_version, minor_version):
            continue
        patch = int((tag[1:] if tag.startswith("v") else tag).split(".")[2])
        if patch < int(current_patch):
            matching_releases.append(release)

    mark_release_as_latest(token, current_release["id"])
    print(f"   [OK] {current_tag} 已设为 latest")

    release_line = f"{major_version}.{minor_version}.x"
    if not matching_releases:
        print(f"   没有找到 {release_line} 版本线内需要清理的旧 Release")
        return

    print(f"   找到 {len(matching_releases)} 个 {release_line} 版本线内的旧 Release，正在删除...")
    for release in matching_releases:
        tag = release["tag_name"]
        release_id = release["id"]
        print(f"   删除 {tag} (ID: {release_id})")
        delete_release(token, release_id)
        delete_tag(token, tag)

    print(f"   [OK] {release_line} 版本线内的旧 Release 已删除")


def create_release(token: str, tag: str, name: str, body: str) -> dict:
    """创建 GitHub Release。"""
    payload = {
        "tag_name": tag,
        "name": name,
        "body": body,
        "draft": False,
        "prerelease": False,
        "make_latest": "false",
    }
    return github_request(token, "POST", "/releases", payload)


def get_release_body(log_path: Path) -> str:
    """生成 markdown 格式的 Release 介绍内容。"""
    release_log = log_path.read_text(encoding="utf-8").strip()
    lines = [f"## MakeCode {CURRENT_VERSION}", ""]
    if release_log:
        lines.extend([
            "**发布日志**:",
            "",
            "<!-- makecode-release-log-start -->",
            release_log,
            "<!-- makecode-release-log-end -->",
            "",
        ])

    lines.append("### 下载")
    lines.append("- `MakeCode-Windows-X64.zip` — Windows X64 完整应用目录")
    lines.append("- `MakeCode-macOS-ARM64.zip` — macOS ARM64 完整应用目录")
    lines.append("- `MakeCode-Linux-X64.zip` — Linux X64 完整应用目录")
    lines.append("- `version.json` — 版本信息文件")

    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--finalize-release",
        metavar="CURRENT_TAG",
        help="资产上传成功后设为 latest，并清理同一次版本线内的旧版本",
    )
    args = parser.parse_args()

    token = get_token()
    tag = f"v{CURRENT_VERSION}"
    if args.finalize_release:
        if args.finalize_release != tag:
            parser.error(
                f"收尾目标 {args.finalize_release} 与当前版本 {tag} 不一致"
            )
        print(f"[收尾] {tag} 资产上传完成，开始发布并清理旧版本...")
        finalize_release(token, tag)
        return

    log_path = Path("RELEASE_LOG.md")

    if not log_path.exists():
        print(f"❌ 找不到 {log_path}")
        sys.exit(1)

    # 生成 Release 介绍内容
    body = get_release_body(log_path)

    # 创建新 Release
    print(f"[创建] Release {tag}...")
    release = create_release(
        token,
        tag=tag,
        name=tag,
        body=body,
    )
    print(f"   Release ID: {release['id']}")
    print(f"   URL: {release['html_url']}")
    print("   Release 暂不设为 latest；Windows/macOS/Linux ZIP 和 version.json 将由 GitHub Actions 生成并上传")

    print()
    print(f"[OK] GitHub Release 发布成功！")
    print(f"   下载地址: {release['html_url']}")

    # 清理发布日志文件
    log_file = Path("RELEASE_LOG.md")
    if log_file.exists():
        log_file.unlink()


if __name__ == "__main__":
    main()
