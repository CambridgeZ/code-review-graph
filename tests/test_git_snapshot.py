"""Git snapshot identities reflect content and scope, not query repetition."""

from __future__ import annotations

import os
import subprocess

import pytest

from code_review_graph import git_snapshot
from code_review_graph.git_snapshot import capture_git_snapshot


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=10,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "snapshot repo"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Snapshot Test")
    git(root, "config", "user.email", "snapshot@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (root / "first.py").write_text("value = 'old'\n", encoding="utf-8")
    (root / "second.py").write_text("other = 'old'\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\0old binary\xff")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    monkeypatch.setenv("CRG_DISCOVERY_TIMEOUT", "10")
    return root


def change(repo, text="value = 'new'\n"):
    (repo / "first.py").write_text(text, encoding="utf-8")


def snapshot(repo, base="HEAD", files=None):
    result = capture_git_snapshot(repo, base, files or ["first.py"])
    assert result is not None
    return result


def test_repeated_identical_content_and_absolute_scope_have_same_identity(repo):
    change(repo)
    first = snapshot(repo)
    assert snapshot(repo) == first
    assert snapshot(repo, files=[str(repo / "first.py")]) == first
    os.utime(repo / "first.py", (1_800_000_000, 1_800_000_000))
    assert snapshot(repo) == first
    assert first["file_count"] == 1
    assert set(first) == {"snapshot_id", "head_sha", "base_sha", "file_count"}
    assert len(first["snapshot_id"]) == 64


def test_same_size_edits_have_different_identity(repo):
    change(repo, "value = 'one'\n")
    first = snapshot(repo)
    change(repo, "value = 'two'\n")
    assert snapshot(repo)["snapshot_id"] != first["snapshot_id"]


def test_staging_unchanged_content_does_not_create_another_snapshot(repo):
    change(repo)
    first = snapshot(repo)
    git(repo, "add", "first.py")
    assert snapshot(repo) == first
    git(repo, "reset", "--", "first.py")
    assert snapshot(repo) == first


def test_combined_staged_and_unstaged_state_is_captured(repo):
    change(repo, "value = 'one'\n")
    git(repo, "add", "first.py")
    staged = snapshot(repo)
    change(repo, "value = 'two'\n")
    both = snapshot(repo)
    assert both["snapshot_id"] != staged["snapshot_id"]
    git(repo, "add", "first.py")
    assert snapshot(repo) == both


def test_same_content_in_new_commit_or_different_base_is_distinct(repo):
    initial = git(repo, "rev-parse", "HEAD")
    change(repo)
    dirty = snapshot(repo)
    git(repo, "add", "first.py")
    git(repo, "commit", "-m", "changed")
    committed = snapshot(repo, base=initial)
    assert committed["snapshot_id"] != dirty["snapshot_id"]
    assert committed["head_sha"] != dirty["head_sha"]
    change(repo, "value = 'two'\n")
    assert snapshot(repo, base=initial)["snapshot_id"] != snapshot(repo)["snapshot_id"]


def test_scope_is_sorted_deduplicated_and_part_of_identity(repo):
    change(repo)
    (repo / "second.py").write_text("other = 'new'\n", encoding="utf-8")
    first_only = snapshot(repo)
    both = snapshot(repo, files=["second.py", "first.py", "first.py"])
    assert both["snapshot_id"] != first_only["snapshot_id"]
    assert both["file_count"] == 2
    assert snapshot(repo, files=["first.py", "second.py"]) == both


def test_unrelated_changes_outside_scope_do_not_change_identity(repo):
    change(repo)
    first = snapshot(repo)
    (repo / "second.py").write_text("other = 'new'\n", encoding="utf-8")
    assert snapshot(repo) == first


def test_deleted_paths_are_identified_before_and_after_staging(repo):
    (repo / "first.py").unlink()
    deleted = snapshot(repo)
    git(repo, "add", "-u")
    assert snapshot(repo) == deleted


def test_staged_rename_is_fingerprinted_as_stable_delete_and_add(repo):
    (repo / "first.py").rename(repo / "renamed.py")
    # A newly-created untracked destination cannot be silently credited.
    assert capture_git_snapshot(repo, "HEAD", ["first.py", "renamed.py"]) is None
    git(repo, "add", "-A")
    renamed = snapshot(repo, files=["first.py", "renamed.py"])
    assert renamed == snapshot(repo, files=["renamed.py", "first.py"])


def test_binary_edits_and_mode_changes_affect_identity(repo):
    (repo / "blob.bin").write_bytes(b"\0new binary\xff")
    binary = snapshot(repo, files=["blob.bin"])
    (repo / "blob.bin").write_bytes(b"\0xxx binary\xff")
    assert snapshot(repo, files=["blob.bin"])["snapshot_id"] != binary["snapshot_id"]
    git(repo, "update-index", "--chmod=+x", "first.py")
    mode = snapshot(repo)
    change(repo)
    assert snapshot(repo)["snapshot_id"] != mode["snapshot_id"]


def test_literal_metacharacter_paths_do_not_expand_scope(repo):
    name = "[literal].py"
    (repo / name).write_text("old\n", encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-m", "literal name")
    (repo / name).write_text("new\n", encoding="utf-8")
    assert snapshot(repo, files=[name])["file_count"] == 1


def test_invalid_untracked_directory_and_outside_scope_are_not_recordable(repo):
    (repo / "untracked.py").write_text("new\n", encoding="utf-8")
    change(repo)
    for files in (
        [], ["untracked.py"], ["first.py", "untracked.py"], ["../outside.py"],
        [str(repo.parent / "outside.py")], ["."], ["\0bad"], ["missing.py"],
    ):
        assert capture_git_snapshot(repo, "HEAD", files) is None
    assert capture_git_snapshot(repo, "does-not-exist", ["first.py"]) is None
    assert capture_git_snapshot(repo, "--help", ["first.py"]) is None
    assert capture_git_snapshot(repo.parent, "HEAD", ["outside.py"]) is None


def test_clean_scope_is_not_a_change(repo):
    assert capture_git_snapshot(repo, "HEAD", ["first.py"]) is None


def test_git_failure_or_timeout_returns_none(repo, monkeypatch):
    change(repo)

    def failure(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 0.01)

    monkeypatch.setattr(git_snapshot.subprocess, "run", failure)
    assert capture_git_snapshot(repo, "HEAD", ["first.py"]) is None


def test_command_line_batches_preserve_every_scoped_path(repo, monkeypatch):
    change(repo)
    (repo / "second.py").write_text("other = 'new'\n", encoding="utf-8")
    monkeypatch.setattr(git_snapshot, "_PATH_ARGUMENT_BUDGET", 20)
    first = snapshot(repo, files=["first.py", "second.py"])
    assert first["file_count"] == 2
    assert snapshot(repo, files=["second.py", "first.py"]) == first


def test_capture_never_writes_raw_diff_or_changes_git_index(repo):
    change(repo)
    before_index = (repo / ".git" / "index").read_bytes()
    before_files = {p.relative_to(repo) for p in repo.rglob("*") if p.is_file()}
    snapshot(repo)
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert {p.relative_to(repo) for p in repo.rglob("*") if p.is_file()} == before_files
