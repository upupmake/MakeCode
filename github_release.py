"""
GitHub Release 上传脚本。
用法: python github_release.py

需要设置环境变量 GITHUB_TOKEN，或在同目录下创建 .github_token 文件。
"""
import os
import sys
from pathlib import Path

import requests

from version import CURRENT_VERSION

# GitHub 配置
GITHUB_OWNER = "upupmake"
GITHUB_REPO = "MakeCode"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"


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


def get_all_releases(token: str) -> list:
    """获取所有 Releases。"""
    url = f"{GITHUB_API}/releases"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def delete_release(token: str, release_id: int):
    """删除指定 Release。"""
    url = f"{GITHUB_API}/releases/{release_id}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.delete(url, headers=headers)
    resp.raise_for_status()


def release_tag_matches_major(tag: str, major_version: str) -> bool:
    """判断 tag 是否属于指定主版本。"""
    version = tag[1:] if tag.startswith("v") else tag
    parts = version.split(".")
    return len(parts) == 3 and parts[0] == major_version


def delete_releases_for_major(token: str, major_version: str):
    """删除指定主版本线的 Releases。"""
    releases = get_all_releases(token)
    matching_releases = [
        release
        for release in releases
        if release_tag_matches_major(release["tag_name"], major_version)
    ]

    if not matching_releases:
        print(f"   没有找到 {major_version}.x.x 版本线的 Release")
        return

    print(f"   找到 {len(matching_releases)} 个 {major_version}.x.x 版本线的 Release，正在删除...")
    for release in matching_releases:
        tag = release["tag_name"]
        release_id = release["id"]
        print(f"   删除 {tag} (ID: {release_id})")
        delete_release(token, release_id)

        # 同时删除对应的 tag
        tag_url = f"{GITHUB_API}/git/refs/tags/{tag}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        requests.delete(tag_url, headers=headers)

    print(f"   [OK] {major_version}.x.x 版本线的 Release 已删除")


def create_release(token: str, tag: str, name: str, body: str) -> dict:
    """创建 GitHub Release。"""
    url = f"{GITHUB_API}/releases"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "tag_name": tag,
        "name": name,
        "body": body,
        "draft": False,
        "prerelease": False,
    }

    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


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
    token = get_token()
    tag = f"v{CURRENT_VERSION}"
    log_path = Path("RELEASE_LOG.md")

    if not log_path.exists():
        print(f"❌ 找不到 {log_path}")
        sys.exit(1)

    # 删除当前主版本线的旧 Release，保留其他主版本线的稳定版本
    major_version = CURRENT_VERSION.split(".", 1)[0]
    print(f"[清理] 删除 {major_version}.x.x 版本线的旧 Releases...")
    delete_releases_for_major(token, major_version)

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
    print("   Windows/macOS/Linux ZIP 和 version.json 将由 GitHub Actions 生成并上传")

    print()
    print(f"[OK] GitHub Release 发布成功！")
    print(f"   下载地址: {release['html_url']}")

    # 清理发布日志文件
    log_file = Path("RELEASE_LOG.md")
    if log_file.exists():
        log_file.unlink()


if __name__ == "__main__":
    main()
