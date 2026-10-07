# Mojira Monitor

Monitor Minecraft bug reports by **project, keywords, regular expressions, and issue labels**. Supports independent monitors, SQLite storage, optional AI classification, duplicate removal, and restoration when reports reopen.

Track crashes, performance problems, redstone behavior, world corruption, or any other topic. You can also collect all recent reports for selected projects. Supported sources are the Mojang bug tracker and mojira.dev.

## Quick start

Requires **Python 3.11+ on Linux or macOS**. Use WSL on Windows; process locking uses `fcntl`.

```bash
git clone https://github.com/HackerRouter/mojira-monitor.git
cd mojira-monitor
uv sync --extra dev --frozen
.venv/bin/mojira-monitor monitors
.venv/bin/mojira-monitor doctor
.venv/bin/mojira-monitor once
.venv/bin/mojira-monitor run
```

`monitors` lists all configured monitors, including disabled examples. `doctor` checks source connectivity without collecting data or requiring an AI key. `once` collects and rechecks reports once. `run` continuously schedules all enabled monitors.

Without uv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Only the `crashes` monitor is enabled by default. `config.toml` also includes Bedrock performance and Java redstone examples. Set `enabled=true` to run them automatically. Databases, collected reports, credentials, and local configuration files are excluded from Git.

## Independent monitor configurations

Top-level settings provide shared defaults. Each `[[monitors]]` entry can override projects, source, polling intervals, filters, classification, and AI settings. Sections inherit individual fields; arrays replace inherited arrays entirely. Set an array to `[]` to clear inherited keywords or filters.

```toml
database = "data/mojira.sqlite3"
source = "official"
projects = ["MC", "MCPE", "BDS"]

[polling]
collect_minutes = 20
recheck_hours = 12

[[monitors]]
id = "crashes"
name = "Crash reports"
database = "data/mojira.sqlite3"
tags = ["stability"]

[monitors.classification]
mode = "crash"
include_hangs = false

[[monitors]]
id = "bedrock-performance"
name = "Bedrock performance issues"
projects = ["MCPE", "BDS"]
tags = ["performance", "bedrock"]

[monitors.polling]
collect_minutes = 30

[monitors.classification]
mode = "keywords"
category = "performance"
keywords_any = ["fps", "stutter", "frame rate"]
patterns_any = ['\blag(?:gy|ging)?\b']
keywords_all = []
exclude_keywords = ["feature request"]
fields = ["summary", "description"]

[monitors.filters]
labels_any = []
labels_all = []
exclude_labels = []
```

Each monitor has its own **database, checkpoints, classification cache, recheck schedule, and event history**. A report can belong to several monitors. Removing it from one monitor does not affect the others. If `database` is omitted, a separate path is derived from the shared database name, such as `data/mojira.bedrock-performance.sqlite3`.

Monitor IDs and database paths must be unique within a configuration. A database binds to its monitor ID on first use to prevent another configuration from overwriting its classifications. Keep IDs stable; change `name` to rename a monitor for display. Monitors execute serially at their configured intervals, so a slow task can delay later tasks.

Legacy single-monitor TOML files still work. Without `[[monitors]]`, the file defines one monitor with the ID `default`, or the value of `monitor_id` if supplied. Standalone examples:

- [examples/general.toml](examples/general.toml): world and chunk data corruption.
- [examples/all-java.toml](examples/all-java.toml): recent Java reports without a crash requirement.

Relative database paths are resolved against the **directory containing the TOML file**.

## Projects, labels, and matching

`MC`, `MCPE`, and `BDS` are **project codes** and belong in `projects`. Other valid Mojira project codes can also be configured. Use `doctor` to check whether a project is accessible.

| Setting | Meaning |
|---|---|
| `projects` | Projects to monitor, such as `["MC", "MCPE"]` |
| `tags` | Local grouping tags included as `monitor_tags` in output; these do not modify source issues |
| `filters.labels_any` | Require at least one of these issue labels |
| `filters.labels_all` | Require every listed issue label |
| `filters.exclude_labels` | Exclude reports containing any listed label |
| `classification.keywords_any` | Literal keywords; at least one must match across this list and `patterns_any` combined |
| `classification.patterns_any` | Python regular expressions, including word boundaries |
| `classification.keywords_all` | Also require every listed literal keyword |
| `classification.exclude_keywords` | Exclude reports matching any listed literal keyword |
| `classification.exclude_patterns` | Exclude reports matching any listed regular expression |
| `classification.fields` | Fields to search: `summary`, `description`, and `environment` |
| `classification.case_sensitive` | Case sensitivity for custom keywords and patterns; false by default |
| `classification.category` | Category assigned by general matching, such as `performance` |

Issue labels use **exact, case-sensitive matching**. All nonempty label conditions must pass. Use label names that actually exist in the source reports.

Literal keywords match substrings: `corrupt` also matches `corrupted`. To avoid matching `lag` inside `villager`, use `patterns_any = ['\blag\b']`. Selected fields are joined with newlines before matching. Comments and attachment contents are not searched.

Three classification modes are available:

- `crash`: built-in English crash classification, producing `server_crash`, `client_crash`, `crash_unspecified`, or `hang`. Custom positive keyword conditions can narrow the scope. `extra_patterns` extends the crash rules.
- `keywords`: general topic matching without requiring a crash. Configure at least one positive keyword, pattern, or label condition.
- `all`: store every eligible report that is not a duplicate. Configured keyword, label, and exclusion conditions still apply; clear keyword arrays when no keyword restriction is wanted.

Exclusion keywords and labels take priority. Rule matches are candidates, not confirmation by Mojang. A report that describes both a freeze and a crash may match the crash rules. `include_hangs=false` only excludes reports matching the hang rules alone.

## Running, querying, and exporting

Place global options such as `--config` and `--monitor` before the subcommand. Commands operate on all enabled monitors by default. Explicit `--monitor` options can select disabled examples and can be repeated to select several monitors.

```bash
.venv/bin/mojira-monitor --monitor bedrock-performance once
.venv/bin/mojira-monitor --monitor crashes --monitor java-redstone run
.venv/bin/mojira-monitor --config examples/general.toml once

.venv/bin/mojira-monitor --monitor crashes list --limit 10
.venv/bin/mojira-monitor --monitor crashes list --all --limit 10
.venv/bin/mojira-monitor status
.venv/bin/mojira-monitor --monitor crashes recheck --force
.venv/bin/mojira-monitor --monitor crashes backfill
.venv/bin/mojira-monitor export --output data/reports.json
```

`list --limit` applies separately to each monitor. Each listed or exported record includes the monitor ID, name, tags, report content, versions, source data, and classification evidence. `--all` includes inactive records. Export refuses to overwrite an existing file. Results include statistics for each selected monitor. Exit code 1 indicates at least one failure; 0 indicates success.

Changing keywords, labels, or AI settings invalidates the classification cache but does not immediately rescan all history. Use `backfill` to reassess the configured historical window and `recheck --force` to reassess stored reports. Forced rechecks still respect the batch limit. Removing a project from the configuration stops collection and rechecks for that project; its existing records remain available in the database and exports.

## Incremental collection and status maintenance

| Default setting | Value |
|---|---:|
| `polling.collect_minutes` | 20 minutes |
| `polling.recheck_hours` | 12 hours |
| `polling.duplicate_recheck_hours` | 168 hours |
| `polling.recheck_batch` | 100 reports |
| `polling.page_size` | 50 reports, up to 100 |
| `polling.bootstrap_pages` / `initial_hours` | Initially up to 2 pages per project, considering updates within 24 hours |
| `polling.overlap_hours` | 6 hours |
| `polling.max_pages` | Up to 30 pages per project per incremental or backfill run |
| `polling.backfill_hours` / `backfill_interval_hours` | Rescan the last 168 hours every 24 hours |
| `http.min_interval_seconds` | At least 1 second between requests per HTTP client |

Initial collection intentionally takes a recent subset. Reaching its limit produces `bootstrap_truncated=true`. Subsequent incremental collection that reaches `max_pages` before covering the checkpoint reports an error and keeps the previous checkpoint. Increase the limit to clear the backlog. Individual report failures also prevent the affected project's checkpoint from advancing.

Pagination does not provide a fixed snapshot. Overlap windows, key deduplication, and daily backfills reduce omissions caused by concurrent updates and mirror delays, but do not guarantee a complete historical copy. The first scheduled backfill runs after its configured interval; use `backfill` to run it immediately.

Duplicate removal only occurs when the source resolution is `Duplicate`. Mentions in comments do not trigger removal. The canonical report is identified by the `duplicates` link direction, not `is duplicated by`. Its key is stored in `duplicate_of`; the monitor does not recursively collect it. Reopened reports can become active again if they match. A `Fixed` resolution alone does not remove a report.

HTTP 404, HTTP 403, timeouts, and malformed responses preserve local records and record the failure. Older source responses cannot overwrite newer local state. Regular rechecks reuse classifications for unchanged reports to avoid repeated AI requests.

Existing SQLite tables remain compatible. The default `crashes` monitor continues to use `data/mojira.sqlite3`. A legacy database without a monitor binding is adopted on first use without deleting reports. Core tables are `bugs`, `seen`, `metadata`, `events`, and `runs`.

## Optional AI classification

AI is disabled by default with `ai.mode="off"` and requires no key. Configure a Chat Completions-compatible service through a shared `[ai]` section or a monitor-specific `[monitors.ai]` section:

```toml
[ai]
mode = "candidates"
base_url = "https://YOUR_PROVIDER/v1"
model = "YOUR_MODEL"
api_key_env = "MOJIRA_AI_API_KEY"
min_confidence = 0.75
max_chars = 16000
criteria = "Only track world or chunk data loss caused by the game itself."
```

Provide credentials through the named environment variable rather than TOML. The program does not load `.env` automatically. Enabling AI sends public report keys, labels, and truncated text from selected fields to the configured service.

- `candidates`: analyze rule candidates to reduce false positives. This cannot recover reports missed by every keyword rule.
- `all`: analyze every new or changed report that passes project, label, and exclusion filters. Semantic matching can go beyond literal positive keywords and requires more requests.
- Crash mode classifies crashes. General modes assess relevance using the configured keywords and optional `criteria`, then assign the configured `category`.
- AI cannot override project, label, or exclusion constraints. Duplicate handling always uses source status.
- Responses must contain valid categories, confidence values, and quotes from the supplied text. Low confidence or `uncertain` falls back to the rules. Invalid formats or evidence remain eligible for retry rather than being cached as irrelevant.

AI integration is tested with simulated responses. No real model credentials or paid requests were used during development.

## Sources, deployment, and extension

The default source is `POST https://bugs.mojang.com/api/jql-search-post`. MC, MCPE, and BDS were verified on October 7, 2026. Pagination uses a zero-based `page` parameter; sending `nextPageToken` repeated the first page during verification. The program checks returned page numbers and duplicate pages.

mojira.dev triggered a Cloudflare challenge in the development environment. Use `source="mirror"` from an environment with access, or point `mirror_url` at a self-hosted instance. Mirrors may lag behind the official source. The mirror's v1 detail endpoint does not include issue links, comment lists, or attachment lists. The official adapter stores public issue links and attachment metadata without downloading attachments or accessing private reports.

References: [mojira.dev OpenAPI](https://github.com/misode/mojira.dev/blob/main/openapi.yaml) and [official endpoint client](https://github.com/misode/mojira.dev/blob/main/api/public.go). The official website endpoint may change; this project cannot guarantee its stability as a developer API.

`once` performs a single run. `run` schedules monitors while its process remains alive. For long-term deployment, adapt the [systemd template](deploy/mojira-monitor.service) to your service account and installation path. The template is not installed or enabled automatically. Alternatively, use cron:

```cron
*/20 * * * * cd /opt/mojira-monitor && .venv/bin/mojira-monitor once >> data/cron.log 2>&1
15 3 * * * cd /opt/mojira-monitor && .venv/bin/mojira-monitor backfill >> data/cron.log 2>&1
```

Run `init` first to create the data directory. With cron, the cron schedule determines collection frequency, while rechecks still respect their configured intervals. Use `run` or separate cron entries with `--monitor` for different collection frequencies. Do not run cron and the daemon against the same database simultaneously; file locks reject conflicting writers. Separate databases can run independently. Configure log rotation for your deployment.

See [docs/EXTENDING.md](docs/EXTENDING.md) for development guidance. Run tests with:

```bash
.venv/bin/pytest -q
```

Tests run without network access and cover source adapters, pagination recovery, duplicate lifecycles, configuration inheritance, state isolation, keyword and label matching, AI evidence validation, CLI exports, and independent polling intervals. Use `doctor` and `once` for live API verification.
