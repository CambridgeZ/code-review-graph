"""Local token estimates, deduplicated by observed Git change snapshot."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_snapshots (
    repo TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observations INTEGER NOT NULL,
    last_event_id TEXT NOT NULL,
    client TEXT NOT NULL,
    tool TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    baseline_tokens INTEGER NOT NULL,
    returned_tokens INTEGER NOT NULL,
    head_matches_build INTEGER,
    PRIMARY KEY (repo, snapshot_id)
);
CREATE INDEX IF NOT EXISTS usage_repo_time ON usage_snapshots(repo, first_seen_at);
CREATE TABLE IF NOT EXISTS usage_observations (
    repo TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (repo, event_id)
);
"""
_active: ContextVar[UsageCapture | None] = ContextVar("crg_usage_capture", default=None)


def tracking_enabled() -> bool:
    return os.environ.get("CRG_TRACK_USAGE", "").strip().lower() in {"1", "true", "yes"}


@dataclass
class UsageCapture:
    """A request's measurement; only its caller persists a completed result."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.perf_counter)
    baseline_tokens: int | None = None
    snapshot: dict | None = None
    root: Path | None = None
    base: str = "HEAD~1"
    files: list[str] = field(default_factory=list)
    observed_at: str | None = None

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def verify(self) -> None:
        """Skip an analysis if its source files changed while it was running."""
        from .git_snapshot import capture_git_snapshot

        if self.snapshot is not None and self.root is not None:
            after = capture_git_snapshot(self.root, self.base, self.files)
            if after is None or after["snapshot_id"] != self.snapshot["snapshot_id"]:
                self.snapshot = None
                logger.warning("Token usage skipped: Git state changed during analysis")
        self.observed_at = datetime.now(timezone.utc).isoformat()


def capture_baseline(tokens: int, kind: str) -> None:
    """Keep the existing tiny public savings metadata unchanged."""
    current = _active.get()
    if current is not None and tokens > 0 and kind == "full_files":
        current.baseline_tokens = tokens


def capture_change(root: Path, base: str, files: list[str]) -> None:
    """Capture the change analyzer's actual resolved diff scope."""
    from .git_snapshot import capture_git_snapshot

    current = _active.get()
    if current is not None:
        current.root, current.base, current.files = root, base, list(files)
        current.snapshot = capture_git_snapshot(root, base, files)
        if current.snapshot is None:
            logger.warning("Token usage skipped: no stable tracked Git change snapshot")


@contextmanager
def capture_cli_change(root: Path, base: str, files: list[str]) -> Iterator[UsageCapture | None]:
    """CLI change analysis is recorded only when enabled in its environment."""
    if not tracking_enabled():
        yield None
        return
    capture = UsageCapture()
    with capture.activate():
        capture_change(root, base, files)
        yield capture
        capture.verify()


def _location(repo_root: str | None) -> tuple[str, Path]:
    from .incremental import get_data_dir
    from .tools._common import _resolve_root

    root = _resolve_root(repo_root)
    return os.path.normcase(str(root.resolve())), get_data_dir(root, create=False) / "usage.db"


def record_usage(
    tool_name: str, result: dict, repo_root: str | None,
    capture: UsageCapture | None, duration_ms: int | None = None,
) -> dict:
    """Retry brief cross-process WAL initialization/commit contention only."""
    for attempt in range(3):
        try:
            return _record_usage(tool_name, result, repo_root, capture, duration_ms)
        except sqlite3.OperationalError as exc:
            # sqlite_errorcode is only available from Python 3.11 onward.
            code = getattr(exc, "sqlite_errorcode", None)
            busy = (code & 0xFF) in (5, 6) if code is not None else str(exc).lower().startswith(
                ("database is locked", "database table is locked"),
            )
            if not busy or attempt == 2:
                logger.warning("Could not record local token usage: %s", exc)
                return {}
            time.sleep(0.01 * (attempt + 1))
    return {}


def _record_usage(
    tool_name: str, result: dict, repo_root: str | None,
    capture: UsageCapture | None, duration_ms: int | None,
) -> dict:
    """Upsert one successful estimate per repository/snapshot, best effort.

    Repeated observations replace the latest estimate instead of adding it to
    totals. A failed ledger write must never turn a graph answer into an error.
    """
    from .context_savings import estimate_tokens

    if (capture is None or capture.snapshot is None or capture.baseline_tokens is None
            or result.get("status") != "ok"):
        return {}
    try:
        repo, path = _location(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = capture.snapshot
        graph = result.get("_graph") or {}
        matches = graph.get("head_matches_build") if isinstance(graph, dict) else None
        now = capture.observed_at or datetime.now(timezone.utc).isoformat()
        duration = duration_ms if duration_ms is not None else round(
            (time.perf_counter() - capture.started) * 1000,
        )
        connection = sqlite3.connect(path, timeout=0.25)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            inserted = connection.execute(
                "INSERT OR IGNORE INTO usage_observations VALUES (?, ?)",
                (repo, capture.event_id),
            )
            if inserted.rowcount == 0:
                return {}
            connection.execute(
                "INSERT INTO usage_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo, snapshot_id) DO UPDATE SET "
                "first_seen_at=MIN(usage_snapshots.first_seen_at, excluded.first_seen_at), "
                "last_seen_at=MAX(usage_snapshots.last_seen_at, excluded.last_seen_at), "
                "observations=usage_snapshots.observations+1, "
                "last_event_id=CASE WHEN excluded.last_seen_at >= usage_snapshots.last_seen_at "
                "THEN excluded.last_event_id ELSE usage_snapshots.last_event_id END, "
                "client=CASE WHEN excluded.last_seen_at >= usage_snapshots.last_seen_at "
                "THEN excluded.client ELSE usage_snapshots.client END, "
                "tool=CASE WHEN excluded.last_seen_at >= usage_snapshots.last_seen_at "
                "THEN excluded.tool ELSE usage_snapshots.tool END, "
                "duration_ms=CASE WHEN excluded.last_seen_at >= usage_snapshots.last_seen_at "
                "THEN excluded.duration_ms ELSE usage_snapshots.duration_ms END, "
                "baseline_tokens=CASE WHEN excluded.last_seen_at >= usage_snapshots.last_seen_at "
                "THEN excluded.baseline_tokens ELSE usage_snapshots.baseline_tokens END, "
                "returned_tokens=CASE WHEN excluded.last_seen_at >= usage_snapshots.last_seen_at "
                "THEN excluded.returned_tokens ELSE usage_snapshots.returned_tokens END, "
                "head_matches_build=CASE WHEN excluded.last_seen_at "
                ">= usage_snapshots.last_seen_at "
                "THEN excluded.head_matches_build ELSE usage_snapshots.head_matches_build END",
                (
                    repo, snapshot["snapshot_id"], snapshot["base_sha"], snapshot["head_sha"],
                    snapshot["file_count"], now, now, capture.event_id,
                    os.environ.get("CRG_USAGE_CLIENT", "unknown")[:64], tool_name[:128],
                    max(0, duration), capture.baseline_tokens, estimate_tokens(result),
                    int(matches) if isinstance(matches, bool) else None,
                ),
            )
            connection.commit()
        finally:
            connection.close()
    except sqlite3.OperationalError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning("Could not record local token usage: %s", exc)
    return {}


_AGGREGATES = """
COUNT(*) AS snapshots,
COALESCE(SUM(observations), 0) AS observations,
COALESCE(SUM(baseline_tokens), 0) AS baseline_tokens,
COALESCE(SUM(returned_tokens), 0) AS returned_tokens,
COALESCE(SUM(baseline_tokens - returned_tokens), 0) AS net_saved_tokens,
COALESCE(SUM(MAX(baseline_tokens - returned_tokens, 0)), 0) AS positive_saved_tokens,
COALESCE(SUM(MAX(returned_tokens - baseline_tokens, 0)), 0) AS extra_tokens,
COALESCE(SUM(duration_ms), 0) AS duration_ms,
COALESCE(SUM(head_matches_build = 0), 0) AS stale_graph_snapshots,
MIN(first_seen_at) AS first_seen_at,
MAX(last_seen_at) AS last_seen_at
"""


def _metrics(row: dict) -> dict:
    baseline = row["baseline_tokens"]
    row["net_saved_percent"] = (
        round(row["net_saved_tokens"] * 100 / baseline, 2) if baseline else None
    )
    return row


def get_usage_stats(
    repo_root: str | None = None, days: int | None = None, client: str | None = None,
) -> dict:
    """Read distinct-snapshot estimates without opening or updating the graph."""
    if days is not None and (type(days) is not int or not 1 <= days <= 3660):
        return {"status": "error", "error": "days must be an integer from 1 to 3660"}
    if client is not None and (not isinstance(client, str) or not 1 <= len(client) <= 64):
        return {"status": "error", "error": "client must contain 1 to 64 characters"}
    try:
        repo, path = _location(repo_root)
        since = (
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            if days is not None else None
        )
        where = "repo = ?"
        parameters: list = [repo]
        if since is not None:
            where += " AND first_seen_at >= ?"
            parameters.append(since)
        if client is not None:
            where += " AND client = ?"
            parameters.append(client)
        exists = path.exists()
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro" if exists else ":memory:",
            uri=exists, timeout=0.25,
        )
        try:
            connection.row_factory = sqlite3.Row
            if not exists:
                connection.executescript(_SCHEMA)
            connection.execute("BEGIN")
            totals = _metrics(dict(connection.execute(
                f"SELECT {_AGGREGATES} FROM usage_snapshots WHERE {where}", parameters,
            ).fetchone()))
            breakdowns: dict[str, Any] = {}
            for name, expression, limit in (
                ("by_tool", "tool", 50), ("by_client", "client", 50),
                ("by_day", "substr(first_seen_at, 1, 10)", 31),
            ):
                order = "bucket DESC" if name == "by_day" else "snapshots DESC, bucket"
                rows = connection.execute(
                    f"SELECT {expression} AS bucket, {_AGGREGATES} FROM usage_snapshots "
                    f"WHERE {where} GROUP BY {expression} ORDER BY {order} LIMIT ?",
                    [*parameters, limit + 1],
                ).fetchall()
                breakdowns[name] = [_metrics(dict(row)) for row in rows[:limit]]
                breakdowns[f"{name}_truncated"] = len(rows) > limit
            recent = connection.execute(
                "SELECT snapshot_id, base_sha, head_sha, file_count, first_seen_at, last_seen_at, "
                "observations, client, tool, baseline_tokens, returned_tokens, "
                "baseline_tokens-returned_tokens AS net_saved_tokens FROM usage_snapshots "
                f"WHERE {where} ORDER BY first_seen_at DESC, snapshot_id LIMIT 21", parameters,
            ).fetchall()
        finally:
            connection.close()
        return {
            "status": "ok", "estimated": True, "tracking_enabled": tracking_enabled(),
            "ledger_path": str(path), "since_utc": since, "client_filter": client,
            "method": "chars/4; full changed files vs latest analysis payload per Git snapshot",
            "totals": totals, **breakdowns,
            "recent_snapshots": [dict(row) for row in recent[:20]],
            "recent_snapshots_truncated": len(recent) > 20,
            "limitations": (
                "Observed change analyses only, not every edit or all graph queries. "
                "Repeated snapshots replace estimates; distinct snapshots can overlap in content. "
                "Unknown baselines, failures and unstable Git states are excluded. "
                "Not billed tokens, cache-adjusted cost, or measured time saved. "
                "Negative net savings are retained. Recording failures are logged on stderr."
            ),
        }
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {"status": "error", "error": f"Could not read local token usage: {exc}"}
