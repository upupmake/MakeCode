import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

import github_release


def _assets(*names: str) -> list[dict[str, str]]:
    return [{"name": name} for name in names]


def test_release_tag_matches_minor_version_line():
    assert github_release.release_tag_matches_minor("v6.1.8", "6", "1")
    assert github_release.release_tag_matches_minor("6.1.7", "6", "1")
    assert not github_release.release_tag_matches_minor("v6.0.9", "6", "1")
    assert not github_release.release_tag_matches_minor("v5.1.9", "6", "1")
    assert not github_release.release_tag_matches_minor("v6.1", "6", "1")
    assert not github_release.release_tag_matches_minor("v6.1.beta", "6", "1")


def test_finalize_only_deletes_older_patches_after_current_assets_are_complete():
    releases = [
        {
            "id": 618,
            "tag_name": "v6.1.8",
            "assets": _assets(*github_release.REQUIRED_RELEASE_ASSETS),
        },
        {"id": 617, "tag_name": "v6.1.7", "assets": []},
        {"id": 619, "tag_name": "v6.1.9", "assets": []},
        {"id": 604, "tag_name": "v6.0.4", "assets": []},
        {"id": 518, "tag_name": "v5.1.8", "assets": []},
    ]

    with (
        patch("github_release.get_all_releases", return_value=releases),
        patch("github_release.delete_release") as delete_release,
        patch("github_release.delete_tag") as delete_tag,
        patch("github_release.mark_release_as_latest") as mark_latest,
    ):
        github_release.finalize_release("token", "v6.1.8")

    mark_latest.assert_called_once_with("token", 618)
    delete_release.assert_called_once_with("token", 617)
    delete_tag.assert_called_once_with("token", "v6.1.7")


def test_finalize_stops_before_latest_or_deletion_when_current_assets_are_incomplete():
    releases = [
        {
            "id": 618,
            "tag_name": "v6.1.8",
            "assets": _assets("MakeCode-Windows-X64.zip"),
        },
        {"id": 617, "tag_name": "v6.1.7", "assets": []},
    ]

    with (
        patch("github_release.get_all_releases", return_value=releases),
        patch("github_release.delete_release") as delete_release,
        patch("github_release.delete_tag") as delete_tag,
        patch("github_release.mark_release_as_latest") as mark_latest,
        pytest.raises(RuntimeError, match="发布资产尚未齐全"),
    ):
        github_release.finalize_release("token", "v6.1.8")

    mark_latest.assert_not_called()
    delete_release.assert_not_called()
    delete_tag.assert_not_called()


def test_new_release_is_not_latest_until_workflow_finalizes_it():
    with patch("github_release.github_request") as request:
        github_release.create_release("token", "v6.1.8", "v6.1.8", "body")

    request.assert_called_once_with(
        "token",
        "POST",
        "/releases",
        {
            "tag_name": "v6.1.8",
            "name": "v6.1.8",
            "body": "body",
            "draft": False,
            "prerelease": False,
            "make_latest": "false",
        },
    )


def test_release_workflow_finalizes_only_after_uploading_assets():
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    upload_index = workflow.index("- name: Create or update GitHub Release")
    cleanup_index = workflow.index("- name: Finalize release after assets are available")

    assert upload_index < cleanup_index
    assert "make_latest: false" in workflow
    assert 'python github_release.py --finalize-release "${{ github.ref_name }}"' in workflow


def test_tree_sitter_dependencies_are_pinned_to_compatible_versions():
    requirements = Path("requirements.txt").read_text(encoding="utf-8").splitlines()
    workflow = Path(".github/workflows/build.yml").read_text(encoding="utf-8")

    assert "tree-sitter==0.25.2" in requirements
    assert "tree-sitter-language-pack==1.14.3" in requirements
    assert not any(
        "tree-sitter" in line and ";" in line
        for line in requirements
    )
    assert (
        "git+https://github.com/xberg-io/tree-sitter-language-pack.git"
        "@df3bcc39862da6972032d7537d49b782a50a25bb"
        "#subdirectory=packages/python"
        in workflow
    )


def test_bundled_tree_sitter_parsers_match_pinned_release():
    expected_hashes = {
        "parsers-linux-x86_64.tar.zst": "935c0990f08cde9f41ff5519de5129b6b73acebcc80a6db647a1aadf5ca19a77",
        "parsers-macos-arm64.tar.zst": "7097f715d07688e6c12740908c712e67d5672aeb05971dec3b65d19cf7080159",
        "parsers-windows-x86_64.tar.zst": "03e64093297c3bde2139704af580fc80e34eac1bb08b98f737a325a6f6ee6766",
    }

    for file_name, expected_hash in expected_hashes.items():
        archive = Path("ts_cache") / file_name
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_hash
