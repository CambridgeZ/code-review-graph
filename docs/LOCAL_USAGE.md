# Local token estimates by Git change

The local usage ledger records a token estimate for each distinct Git change
snapshot observed during analysis. It lets you compare repository totals over
time without counting the same change again every time you inspect it.

Collection is opt-in. Set `CRG_TRACK_USAGE=1` in the environment of the MCP
server or CLI process. Optionally set `CRG_USAGE_CLIENT=codex` or `claude` to
label the process. Reconnect an existing MCP server after changing its
environment or executable. If the server uses a `--tools` allowlist, add
`get_usage_stats_tool` to that list.

```powershell
$env:CRG_TRACK_USAGE = '1'
$env:CRG_USAGE_CLIENT = 'terminal'
code-review-graph detect-changes --brief --repo Q:/Edge/src
code-review-graph usage-stats --repo Q:/Edge/src
code-review-graph usage-stats --repo Q:/Edge/src --days 7 --json
```

## What creates a record

Successful `detect_changes_tool` MCP calls, `detect-changes` CLI commands, and
the analysis performed by `update --brief` can record a snapshot when both its
Git identity and token baseline are available. Collection observes these
analyses; it does **not** add a filesystem watcher or capture every intermediate
edit. Failed, timed-out, empty, and unmeasurable analyses add no savings record.
Enabling collection starts new history and does not import earlier log estimates.
Untracked files are excluded; new files become eligible after they are added to
Git and included in the analysed change scope.

A snapshot includes the repository, resolved comparison revision, current HEAD,
the selected file scope, and a fingerprint of the tracked Git changes. A changed
diff, comparison revision, or HEAD produces a different snapshot. Repeating an
analysis of the same snapshot updates its latest successful measurement rather
than increasing the number of snapshots or adding its savings again. The first
observation time is retained. Labels identify the latest measurement, so a
snapshot observed by both Codex and Claude is still counted once.

Statistics queries themselves are never recorded. The ledger is `usage.db`
beside `graph.db`, respecting `CRG_DATA_DIR` and the repository registry. It uses
a separate SQLite database and supports multiple local processes sharing it.
Querying statistics neither scans nor rebuilds the graph. An empty history query
does not create a ledger. Set `CRG_TRACK_USAGE=0` and reconnect to stop collection;
the existing history remains readable.

## Querying and interpreting the report

Use `get_usage_stats_tool(repo_root=None, days=None, client=None)` over MCP, or
`usage-stats` over the CLI. Omit `days` for all history. `days=7` selects snapshots
first observed within the last seven days, using UTC. `client` selects snapshots
whose latest successful measurement carries that client label.

The JSON report contains repository totals, bounded tool/client/day breakdowns,
and recent snapshots. Its totals count distinct snapshots in the selected window;
the `observations` count also reveals repeated measurements. It includes the
full-file token baseline, estimated analysis payload size, net saved tokens, extra
tokens, and the duration and graph freshness of the latest measurement. Duration
totals sum those retained measurements, not every repeated observation.

Net savings retain negative differences when a response is larger than its
baseline. The percentage is calculated from summed token counts rather than
averaging per-snapshot percentages. Only recorded snapshots are represented;
failure to write the ledger logs a warning and does not fail the code analysis.

These remain **context-size estimates**, not billing or productivity measurements:

- The approximation is roughly four characters per token; full-file baselines
  use file byte sizes. It assumes those changed files would otherwise be read
  in full. It does not observe how much source an agent actually needed.
- Changing the same file repeatedly can create successive snapshots with
  overlapping source content. Their sum describes the observed change snapshots,
  not a count of unique source tokens avoided across an entire project.
- A repeated snapshot keeps its latest measurement, so totals can change after
  a graph refresh or a different analysis detail level without adding a snapshot.
- Model tokenization, prompt caching, follow-up file reads, conversation replays,
  and actual API billing are outside this ledger. Recorded duration is time spent
  analysing, not time saved.
- CLI estimates use the underlying analysis JSON, including for `--brief`, which
  prints a shorter panel. They are not measurements of the bytes printed to the
  terminal or MCP wire framing.
- A small payload does not prove that the graph indexed every changed file or
  returned complete or correct review context; token size and analysis quality
  are separate measurements.

No source text, raw diff, prompts, or returned text is stored in the ledger.
Only local repository/change identity, labels, timestamps, status metadata and
estimated counts are persisted. Nothing is sent to a cloud service. Measuring
actual time or billing improvements needs comparable tasks run with and without
the graph.
