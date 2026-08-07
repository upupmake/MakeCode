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
