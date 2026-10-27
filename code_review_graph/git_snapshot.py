"""Read-only fingerprints of scoped Git changes for the local usage ledger.

Only the digest and revision identifiers leave this module. Patch bytes are
streamed into a hash, rather than retained in the ledger or a temporary file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .constants import discovery_timeout

logger = logging.getLogger(__name__)
_OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_PATH_ARGUMENT_BUDGET = 12_000
_GIT_PREFIX = [
    "git", "--literal-pathspecs", "-c", "core.quotePath=true",
    "-c", "core.fsmonitor=false", "-c", "diff.algorithm=myers",
]


def _scope(root: Path, changed_files: list[str]) -> list[str] | None:
    """Accept literal file names, including deleted paths and file symlinks."""
    paths = set()
    for name in changed_files:
        if not isinstance(name, str) or not name or "\0" in name:
            return None
        path = Path(name.replace("\\", "/"))
        path = path if path.is_absolute() else root / path
        # Resolve directory aliases/traversal while retaining a final symlink:
        # Git tracks the symlink itself, not its target's file contents.
        path = path.parent.resolve() / path.name
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            return None
        if relative == "." or (path.is_dir() and not path.is_symlink()):
            return None
        if len(relative.encode("utf-16-le")) // 2 > _PATH_ARGUMENT_BUDGET:
            return None
        paths.add(relative)
    return sorted(paths) if paths else None


def _batches(paths: list[str]) -> list[list[str]]:
    """Keep every invocation below Windows' command-line size limit."""
    batches: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for path in paths:
        width = len(path.encode("utf-16-le")) // 2 + 3
        if batch and size + width > _PATH_ARGUMENT_BUDGET:
            batches.append(batch)
            batch, size = [], 0
        batch.append(path)
        size += width
    if batch:
        batches.append(batch)
    return batches


def _git(root: Path, args: list[str], deadline: float) -> bytes:
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise subprocess.TimeoutExpired("git snapshot", 0)
    return subprocess.run(
        [*_GIT_PREFIX, *args], cwd=root, check=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=timeout,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


def _revision(root: Path, ref: str, deadline: float) -> str:
    # --end-of-options also rejects refs that would otherwise be Git options.
    value = _git(root, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
                 deadline).strip().decode("ascii")
    if not _OBJECT_ID.fullmatch(value):
        raise ValueError("Git did not return a commit object ID")
    return value


def _tracked(root: Path, base_sha: str, batch: list[str], deadline: float) -> bool:
    names = set()
    for args in (
        ["ls-tree", "-r", "-z", base_sha, "--", *batch],
        ["ls-files", "--stage", "-z", "--", *batch],
    ):
        for row in _git(root, args, deadline).split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            fields = metadata.split()
            if fields[0] == b"160000":
                return False  # A dirty submodule is not a fingerprint of its contents.
            if args[0] == "ls-files" and fields[2] != b"0":
                return False  # Unmerged index entries have no single review state.
            names.add(name.decode("utf-8", errors="surrogateescape"))
    return set(batch).issubset(names)


def _diff_digest(root: Path, base_sha: str, batch: list[str], deadline: float) -> str | None:
    """Hash a patch with bounded subprocess lifetime and bounded memory."""
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise subprocess.TimeoutExpired("git snapshot", 0)
    process = subprocess.Popen(
        [*_GIT_PREFIX, "diff", "--binary", "--full-index", "--no-ext-diff",
         "--no-textconv", "--no-renames", "--no-color", "--no-relative",
         "--no-indent-heuristic", "--unified=0", "--src-prefix=a/", "--dst-prefix=b/",
         "--ignore-submodules=none", base_sha, "--", *batch],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    digest = hashlib.sha256()
    finished = threading.Event()
    read_errors: list[Exception] = []
    byte_count = 0

    def consume() -> None:
        nonlocal byte_count
        assert process.stdout is not None
        try:
            with process.stdout:
                for chunk in iter(lambda: process.stdout.read(64 * 1024), b""):
                    digest.update(chunk)
                    byte_count += len(chunk)
        except (OSError, ValueError) as exc:
            read_errors.append(exc)
        finally:
            finished.set()

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
        finished.wait(1)
        raise
    if not finished.wait(max(0, deadline - time.monotonic())):
        raise subprocess.TimeoutExpired("git snapshot output", timeout)
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, "git diff")
    if read_errors:
        raise OSError("Could not read Git snapshot output") from read_errors[0]
    return digest.hexdigest() if byte_count else None


def capture_git_snapshot(
    repo_root: Path, base: str, changed_files: list[str],
) -> dict | None:
    """Identify the selected tracked change from an immutable base to disk.

    The identity includes HEAD, the resolved base, sorted literal file scope,
    and content/mode patches. Moving unchanged content between the index and
    working tree does not create another snapshot. Untracked/outside paths,
    missing revisions, conflicts, empty diffs, and Git failures return None.
    ``file_count`` is the number of files in the supplied normalized scope.

    The total fingerprint operation shares the change-discovery time budget.
    Call before and after analysis and compare IDs to reject an analysis that
    overlaps a working-tree edit. This helper alone is not a filesystem lock.
    """
    try:
        root = Path(repo_root).resolve()
        if not root.is_dir() or not isinstance(base, str) or not base or "\0" in base:
            return None
        scope = _scope(root, changed_files)
        if scope is None:
            return None
        deadline = time.monotonic() + discovery_timeout()
        head_sha = _revision(root, "HEAD", deadline)
        base_sha = _revision(root, base, deadline)
        patches = []
        for batch in _batches(scope):
            if not _tracked(root, base_sha, batch, deadline):
                return None
            patches.append(_diff_digest(root, base_sha, batch, deadline))
        if not any(patches) or _revision(root, "HEAD", deadline) != head_sha:
            return None
        # Re-resolve symbolic bases too: a concurrent fetch/ref update can
        # change the intended comparison without changing this worktree's HEAD.
        if _revision(root, base, deadline) != base_sha:
            return None
        identity = json.dumps(
            ["crg-git-snapshot-v1", head_sha, base_sha, scope, patches],
            ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")
        return {
            "snapshot_id": hashlib.sha256(identity).hexdigest(),
            "head_sha": head_sha, "base_sha": base_sha, "file_count": len(scope),
        }
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        logger.debug("Git snapshot unavailable: %s", exc)
        return None
