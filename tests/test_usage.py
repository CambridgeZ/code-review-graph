"""Persistence and accounting contracts for observed Git change snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from code_review_graph import cli, main, usage
from code_review_graph.context_savings import attach_context_savings, estimate_tokens
from code_review_graph.usage import (
    UsageCapture,
    capture_change,
    capture_cli_change,
    get_usage_stats,
    record_usage,
)


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.name=Usage Tests",
         "-c", "user.email=usage-tests@example.invalid", *args],
        cwd=root, check=True, capture_output=True, timeout=10,
    )


def make_repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "--quiet")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.hooksPath", str(root / "no-hooks"))
    for name in ("source.py", "other.py"):
        (root / name).write_text("value = 0\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "base")
    for name in ("source.py", "other.py"):
        (root / name).write_text("value = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRG_TRACK_USAGE", "1")
    monkeypatch.setenv("CRG_USAGE_CLIENT", "test")
    monkeypatch.delenv("CRG_TOOL_TIMEOUT", raising=False)
    return make_repo(tmp_path / "repo")


def answer(repo: Path, baseline=1000, body="compact", files=None):
    capture_change(repo, "HEAD", files or ["source.py"])
    return attach_context_savings(
        {"status": "ok", "summary": body}, original_tokens=baseline,
    )


def measured(repo: Path, baseline=1000, body="compact", files=None):
    capture = UsageCapture()
    with capture.activate():
        result = answer(repo, baseline, body, files)
        capture.verify()
    assert capture.snapshot is not None
    return capture, result


def add(repo: Path, baseline=1000, body="compact", files=None):
    capture, result = measured(repo, baseline, body, files)
    record_usage("test_tool", result, str(repo), capture, 5)
    return capture, result


def test_empty_read_does_not_create_ledger_or_graph(repo, monkeypatch):
    monkeypatch.setenv("CRG_TRACK_USAGE", "0")
    report = get_usage_stats(str(repo))
    assert report["status"] == "ok"
    assert report["totals"]["snapshots"] == 0
    assert report["totals"]["net_saved_percent"] is None
    assert not (repo / ".code-review-graph").exists()
    assert not (repo.parent / "data").exists()


def test_repeated_snapshot_replaces_estimate_and_keeps_first_seen(repo):
    add(repo, 1000)
    first = get_usage_stats(str(repo))["recent_snapshots"][0]
    _, latest = add(repo, 2000, "a longer latest response")
    report = get_usage_stats(str(repo))
    row = report["recent_snapshots"][0]
    assert report["totals"]["snapshots"] == 1
    assert report["totals"]["observations"] == 2
    assert row["first_seen_at"] == first["first_seen_at"]
    assert row["last_seen_at"] >= first["last_seen_at"]
    assert report["totals"]["baseline_tokens"] == 2000
    assert report["totals"]["returned_tokens"] == estimate_tokens(latest)


def test_new_same_size_edit_creates_another_snapshot(repo):
    add(repo, 1000)
    source = repo / "source.py"
    before_size = source.stat().st_size
    source.write_text("value = 2\n", encoding="utf-8")
    assert source.stat().st_size == before_size
    add(repo, 2000)
    totals = get_usage_stats(str(repo))["totals"]
    assert totals["snapshots"] == totals["observations"] == 2
    assert totals["baseline_tokens"] == 3000


def test_retrying_same_event_does_not_increment_observations(repo):
    capture, result = add(repo)
    _, latest = add(repo, 2000)
    record_usage("test_tool", result, str(repo), capture, 5)
    totals = get_usage_stats(str(repo))["totals"]
    assert totals["observations"] == 2
    assert totals["baseline_tokens"] == 2000
    assert totals["returned_tokens"] == estimate_tokens(latest)


def test_out_of_order_writer_keeps_newest_estimate_and_earliest_observation(repo, monkeypatch):
    capture, result = measured(repo)
    now = datetime.now(timezone.utc)
    early, late = (now - timedelta(hours=1)).isoformat(), now.isoformat()
    older = replace(capture, event_id=uuid.uuid4().hex, observed_at=early)
    newer = replace(capture, event_id=uuid.uuid4().hex, observed_at=late, baseline_tokens=2000)
    monkeypatch.setenv("CRG_USAGE_CLIENT", "newest")
    record_usage("new_tool", result, str(repo), newer)
    monkeypatch.setenv("CRG_USAGE_CLIENT", "older")
    record_usage("old_tool", result, str(repo), older)
    row = get_usage_stats(str(repo))["recent_snapshots"][0]
    assert row["first_seen_at"] == early
    assert row["last_seen_at"] == late
    assert row["baseline_tokens"] == 2000
    assert row["tool"] == "new_tool"
    assert row["client"] == "newest"
    assert row["observations"] == 2


@pytest.mark.asyncio
async def test_completed_mcp_counts_final_payload_without_changing_it(repo, monkeypatch):
    expected = answer(repo)
    graph = {"head_matches_build": False, "built_at_sha": "abc"}
    monkeypatch.setattr(main, "with_provenance", lambda result, root: {**result, "_graph": graph})
    result = await main._offload("review_tool", lambda: answer(repo), str(repo))
    assert result == {**expected, "_graph": graph}
    totals = get_usage_stats(str(repo))["totals"]
    assert totals["snapshots"] == totals["observations"] == 1
    assert totals["returned_tokens"] == estimate_tokens(result)
    assert totals["net_saved_tokens"] == 1000 - estimate_tokens(result)
    assert totals["stale_graph_snapshots"] == 1


@pytest.mark.asyncio
async def test_disabled_tracking_never_writes(repo, monkeypatch):
    monkeypatch.delenv("CRG_TRACK_USAGE")
    result = await main._offload("review_tool", lambda: answer(repo), str(repo), provenance=False)
    assert result["status"] == "ok"
    assert get_usage_stats(str(repo))["totals"]["snapshots"] == 0
    assert not (repo.parent / "data").exists()


def test_cli_capture_respects_enabled_setting(repo, monkeypatch):
    with capture_cli_change(repo, "HEAD", ["source.py"]) as capture:
        result = attach_context_savings({"status": "ok"}, original_tokens=1000)
    record_usage("detect-changes", result, str(repo), capture)
    assert get_usage_stats(str(repo))["totals"]["snapshots"] == 1
    monkeypatch.setenv("CRG_TRACK_USAGE", "0")
    with capture_cli_change(repo, "HEAD", ["source.py"]) as capture:
        assert capture is None
    record_usage("detect-changes", result, str(repo), capture)
    assert get_usage_stats(str(repo))["totals"]["observations"] == 1


def test_negative_net_is_retained_and_unknown_or_failed_measurements_are_excluded(repo):
    _, positive = add(repo, 1000)
    _, negative = add(repo, 10, "x" * 1000, ["other.py"])
    capture, result = measured(repo)
    record_usage("failure", {**result, "status": "error"}, str(repo), capture)
    capture.baseline_tokens = None
    record_usage("unknown", result, str(repo), capture)
    capture.baseline_tokens, capture.snapshot = 1000, None
    record_usage("no_snapshot", result, str(repo), capture)
    totals = get_usage_stats(str(repo))["totals"]
    assert totals["snapshots"] == totals["observations"] == 2
    expected_net = 1010 - estimate_tokens(positive) - estimate_tokens(negative)
    assert totals["net_saved_tokens"] == expected_net
    assert totals["extra_tokens"] == estimate_tokens(negative) - 10
    assert totals["positive_saved_tokens"] - totals["extra_tokens"] == totals["net_saved_tokens"]


def test_shared_ledger_isolates_repositories(repo):
    other = make_repo(repo.parent / "other")
    add(repo, 1000)
    add(other, 2000)
    assert get_usage_stats(str(repo))["totals"]["baseline_tokens"] == 1000
    assert get_usage_stats(str(other))["totals"]["baseline_tokens"] == 2000


def test_date_window_uses_first_observation_and_client_uses_latest_label(repo, monkeypatch):
    add(repo, 1000)
    path = repo.parent / "data" / "usage.db"
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE usage_snapshots SET first_seen_at=?", (old,))
    monkeypatch.setenv("CRG_USAGE_CLIENT", "second")
    add(repo, 2000)
    assert get_usage_stats(str(repo), days=7)["totals"]["snapshots"] == 0
    assert get_usage_stats(str(repo), client="test")["totals"]["snapshots"] == 0
    assert get_usage_stats(str(repo), client="second")["totals"]["baseline_tokens"] == 2000
    assert get_usage_stats(str(repo))["recent_snapshots"][0]["first_seen_at"] == old


@pytest.mark.parametrize("days", [0, -1, True, 2.5, 3661, "7"])
def test_invalid_window_does_not_create_a_ledger(repo, days):
    assert get_usage_stats(str(repo), days=days)["status"] == "error"
    assert not (repo.parent / "data").exists()


def test_invalid_root_and_corrupt_ledger_report_errors_not_zero_savings(repo):
    assert get_usage_stats(str(repo / "missing"))["status"] == "error"
    path = repo.parent / "data" / "usage.db"
    path.parent.mkdir()
    path.write_text("not sqlite", encoding="utf-8")
    assert get_usage_stats(str(repo))["status"] == "error"


@pytest.mark.asyncio
async def test_unwritable_ledger_does_not_break_graph_answer(repo, caplog):
    (repo.parent / "data").write_text("not a directory", encoding="utf-8")
    result = await main._offload("review_tool", lambda: answer(repo), str(repo), provenance=False)
    assert result["status"] == "ok"
    assert "Could not record local token usage" in caplog.text


@pytest.mark.asyncio
async def test_timeout_cannot_be_recorded_by_worker_that_finishes_later(repo, monkeypatch):
    monkeypatch.setenv("CRG_TOOL_TIMEOUT", "1")
    release, finished = threading.Event(), threading.Event()

    def slow():
        result = answer(repo)
        release.wait(5)
        finished.set()
        return result

    try:
        result = await main._offload("slow_tool", slow, str(repo), provenance=False)
        assert result["status"] == "error"
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 3)
    assert get_usage_stats(str(repo))["totals"]["snapshots"] == 0
    assert not (repo.parent / "data").exists()


@pytest.mark.asyncio
async def test_exception_is_preserved_without_savings_entry(repo):
    def fail():
        answer(repo)
        raise RuntimeError("failure after baseline")

    with pytest.raises(RuntimeError, match="failure after baseline"):
        await main._offload("failure", fail, str(repo), provenance=False)
    assert get_usage_stats(str(repo))["totals"]["snapshots"] == 0


@pytest.mark.asyncio
async def test_source_edit_during_analysis_prevents_recording(repo, caplog):
    def edited():
        result = answer(repo)
        (repo / "source.py").write_text("value = 2\n", encoding="utf-8")
        return result

    result = await main._offload("review_tool", edited, str(repo), provenance=False)
    assert result["status"] == "ok"
    assert get_usage_stats(str(repo))["totals"]["snapshots"] == 0
    assert "Git state changed during analysis" in caplog.text


@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_snapshot_and_baseline(repo):
    barrier = threading.Barrier(2)
    captures = {}

    def work(name, baseline):
        capture_change(repo, "HEAD", [name])
        barrier.wait(timeout=5)
        result = attach_context_savings({"status": "ok", "summary": name},
                                        original_tokens=baseline)
        captures[name] = result
        return result

    await asyncio.gather(*(
        main._offload(name, lambda name=name, baseline=baseline: work(name, baseline),
                      str(repo), provenance=False)
        for name, baseline in (("source.py", 1000), ("other.py", 2000))
    ))
    rows = {row["tool"]: row for row in get_usage_stats(str(repo))["recent_snapshots"]}
    assert rows["source.py"]["baseline_tokens"] == 1000
    assert rows["other.py"]["baseline_tokens"] == 2000
    assert all(row["returned_tokens"] == estimate_tokens(captures[name])
               for name, row in rows.items())


def test_concurrent_connections_upsert_without_duplicate_snapshots(repo):
    capture, result = add(repo)
    copies = [replace(capture, event_id=uuid.uuid4().hex) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda item: record_usage("test_tool", result, str(repo), item), copies))
    totals = get_usage_stats(str(repo))["totals"]
    assert totals["snapshots"] == 1
    assert totals["observations"] == 13


def test_lock_retry_works_without_python311_sqlite_errorcode(monkeypatch):
    attempts = []

    def flaky(*args):
        attempts.append(None)
        if len(attempts) < 3:
            error = sqlite3.OperationalError("database is locked")
            assert not hasattr(error, "sqlite_errorcode")
            raise error
        return {"recorded": True}

    monkeypatch.setattr(usage, "_record_usage", flaky)
    assert record_usage("test_tool", {}, None, None) == {"recorded": True}
    assert len(attempts) == 3


def test_non_lock_sqlite_error_is_logged_without_retry(monkeypatch, caplog):
    attempts = []

    def broken(*args):
        attempts.append(None)
        raise sqlite3.OperationalError("no such table: usage_snapshots")

    monkeypatch.setattr(usage, "_record_usage", broken)
    assert record_usage("test_tool", {}, None, None) == {}
    assert len(attempts) == 1
    assert "Could not record local token usage" in caplog.text


@pytest.mark.asyncio
async def test_statistics_mcp_and_cli_queries_never_record_themselves(repo, monkeypatch, capsys):
    add(repo)
    tool = getattr(main.get_usage_stats_tool, "fn", main.get_usage_stats_tool)
    for _ in range(2):
        report = await tool(repo_root=str(repo))
        assert report["totals"]["observations"] == 1
        assert "_graph" not in report
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "usage-stats", "--repo",
                                     str(repo), "--json"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["totals"]["observations"] == 1
    assert get_usage_stats(str(repo))["totals"]["observations"] == 1


def test_bounded_breakdowns_and_recent_rows_preserve_exact_totals(repo):
    capture, result = measured(repo)
    # Synthetic historical dates isolate report bounds from fingerprint tests.
    for i in range(35):
        current = replace(capture, event_id=uuid.uuid4().hex,
                          snapshot={**capture.snapshot, "snapshot_id": f"history-{i}"})
        record_usage("history", result, str(repo), current)
        at = (datetime.now(timezone.utc) - timedelta(days=i)).isoformat()
        with sqlite3.connect(repo.parent / "data" / "usage.db") as db:
            db.execute("UPDATE usage_snapshots SET first_seen_at=? WHERE snapshot_id=?",
                       (at, f"history-{i}"))
    report = get_usage_stats(str(repo))
    assert report["totals"]["snapshots"] == 35
    assert report["totals"]["baseline_tokens"] == 35000
    assert len(report["by_day"]) == 31
    assert report["by_day_truncated"] is True
    assert len(report["recent_snapshots"]) == 20
    assert report["recent_snapshots_truncated"] is True


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_real_mcp_and_cli_change_analyses_share_deduplicated_history(repo):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        **os.environ,
        "CRG_PARSE_EXECUTOR": "thread",
        "CRG_PARSE_WORKERS": "2",
        "CRG_GIT_TIMEOUT": "10",
        "CRG_DISCOVERY_TIMEOUT": "10",
        "CRG_TOOL_TIMEOUT": "10",
        "PYTHONUTF8": "1",
    }
    env.pop("CRG_TOOLS", None)

    async def run_cli(*args):
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "code_review_graph", *args, "--repo", str(repo)],
            cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", timeout=40,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    await run_cli("build", "--skip-flows")
    assert (repo.parent / "data" / "graph.db").is_file()
    assert get_usage_stats(str(repo))["totals"]["snapshots"] == 0
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "code_review_graph", "serve", "--repo", str(repo), "--tools",
              "detect_changes_tool,get_usage_stats_tool"],
        cwd=str(repo), env=env,
    )

    async with stdio_client(server) as (reader, writer):
        async with ClientSession(
            reader, writer, read_timeout_seconds=timedelta(seconds=20),
        ) as session:
            await asyncio.wait_for(session.initialize(), timeout=20)

            async def call(name, arguments=None):
                response = await session.call_tool(name, arguments or {})
                assert not response.isError
                payload = response.structuredContent
                if payload is None:
                    payload = json.loads(response.content[0].text)
                assert payload["status"] == "ok", payload
                return payload

            registered = {tool.name for tool in (await session.list_tools()).tools}
            assert registered == {"detect_changes_tool", "get_usage_stats_tool"}
            for expected_observations in (1, 2):
                result = await call("detect_changes_tool", {"base": "HEAD",
                                                           "detail_level": "minimal"})
                assert result["changed_file_count"] == 2
                report = await call("get_usage_stats_tool")
                assert report["totals"]["snapshots"] == 1
                assert report["totals"]["observations"] == expected_observations

            assert "Token Savings" in await run_cli("detect-changes", "--brief", "--base", "HEAD")
            report = await call("get_usage_stats_tool")
            assert report["totals"]["snapshots"] == 1
            assert report["totals"]["observations"] == 3

            await run_cli("update", "--brief", "--base", "HEAD", "--skip-flows")
            report = await call("get_usage_stats_tool")
            assert report["totals"]["snapshots"] == 1
            assert report["totals"]["observations"] == 4

            (repo / "source.py").write_text("value = 2\n", encoding="utf-8")
            await run_cli("detect-changes", "--brief", "--base", "HEAD")
            report = await call("get_usage_stats_tool")
            assert report["totals"]["snapshots"] == 2
            assert report["totals"]["observations"] == 5
