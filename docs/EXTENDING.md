# Extending Mojira Monitor

Configuration, source adapters, classification, storage, and scheduling are separate components. Most new monitoring topics only require TOML changes. New platforms require an adapter implementation.

## Configuration and monitors

`load_configs()` in `config.py` parses one or more `Config` objects. Shared sections inherit individual fields, and monitor-specific fields override them. Arrays replace inherited arrays entirely. `select_configs()` handles enabled monitors and explicit CLI selections.

Each monitor uses a separate SQLite database. `Monitor` handles collection and rechecks for one monitor; `cli.py` starts and schedules multiple monitors. Databases bind to monitor IDs to prevent accidental reuse by unrelated configurations.

## Classification

`Classifier.classify(issue)` returns `Decision(category, relevant, evidence, method, confidence)`. The `relevant` flag determines whether a report belongs in the monitor's active set. Evidence should contain source text or clearly identified label conditions.

When adding built-in rules, update configuration validation, classification logic, and tests. `Config.classifier_version()` derives a cache version from rules, projects, filters, and AI settings. Increment `rules_version` whenever built-in classification semantics change.

The AI adapter accepts classification JSON and does not expose tool calls. Reports are treated as untrusted data. Model evidence must appear in the submitted text. Duplicate removal and restoration remain controlled by source status through `Monitor` and the database.

## Source adapters

`Monitor` depends on this interface:

```python
source.identity: str
source.page(project: str, page: int, limit: int) -> Page
source.issue(key: str) -> Issue
```

`source.identity` must be stable because it namespaces checkpoints.

- Internal page numbers start at 1. The adapter converts them to the upstream convention.
- `Page.issues` must be sorted by descending update time. `Page.has_more` must accurately describe pagination.
- Set `Issue.full=False` when a list response contains only summaries. The engine fetches details when needed.
- Supply a valid key, a timezone-aware update timestamp, and an explicit resolution. Missing fields must raise `SourceError`; they must not silently become unresolved or duplicate reports.
- Raise `NotFound` or `SourceError` for temporarily inaccessible reports so the engine preserves local records.
- Use `JsonHTTP` for throttling, retries, and error handling. Do not log credentials or complete error responses.

`Source` currently supports `official` and `mirror`. Issue keys and generated URLs still follow Mojira/Jira conventions. Adding another platform requires changes to source validation, adapter selection, issue identifiers, and URL generation, not just a replacement URL.

## State compatibility

The multi-monitor implementation keeps the original database tables and binds monitor IDs through metadata. Existing databases do not need to be deleted or recreated. Future schema changes should use explicit schema versions and transactional migrations, with a SQLite backup before migration.

The `seen` cache does not store full bodies for unmatched reports. After changing rules, incremental collection, backfill, or stored-report rechecks must fetch content again. The monitor does not automatically retain the complete source history.
