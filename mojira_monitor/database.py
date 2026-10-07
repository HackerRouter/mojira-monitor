from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from .models import Issue, iso, utcnow


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Database:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (
                key TEXT NOT NULL, source TEXT NOT NULL, fingerprint TEXT NOT NULL,
                classifier_version TEXT NOT NULL, checked_at TEXT NOT NULL,
                PRIMARY KEY (key, source)
            );
            CREATE TABLE IF NOT EXISTS bugs (
                key TEXT PRIMARY KEY, project TEXT NOT NULL, summary TEXT NOT NULL,
                category TEXT NOT NULL, active INTEGER NOT NULL,
                resolution TEXT NOT NULL, status TEXT, duplicate_of TEXT,
                removed_reason TEXT, source_updated TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_checked TEXT NOT NULL,
                last_attempt TEXT NOT NULL, last_error TEXT,
                classification_json TEXT NOT NULL, issue_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS bugs_active ON bugs(active, last_attempt);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, at TEXT NOT NULL,
                event TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, result_json TEXT
            );
        """)

    def close(self):
        self.conn.close()

    def bind_monitor(self, monitor_id):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.get_meta("monitor_id")
            if existing is not None and existing != monitor_id:
                raise ValueError(f"This database already belongs to monitor {existing}; {monitor_id} must use a different database")
            self.conn.execute("INSERT OR IGNORE INTO metadata VALUES ('monitor_id',?)", (monitor_id,))

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.conn:
            self.conn.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_bug(self, key):
        row = self.conn.execute("SELECT * FROM bugs WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def fingerprint(issue: Issue):
        value = [issue.updated_date, issue.resolution, issue.status, issue.summary]
        return hashlib.sha256(encode(value).encode()).hexdigest()

    def seen(self, issue, source, version):
        row = self.conn.execute("SELECT fingerprint,classifier_version FROM seen WHERE key=? AND source=?", (issue.key, source)).fetchone()
        return bool(row and row[0] == self.fingerprint(issue) and row[1] == version)

    def mark_seen(self, issue, source, version):
        with self.conn:
            self.conn.execute("""INSERT INTO seen VALUES (?,?,?,?,?) ON CONFLICT(key,source) DO UPDATE SET
                fingerprint=excluded.fingerprint, classifier_version=excluded.classifier_version,
                checked_at=excluded.checked_at""", (issue.key, source, self.fingerprint(issue), version, iso(utcnow())))

    def save_bug(self, issue, decision):
        previous = self.get_bug(issue.key)
        if previous is None and (issue.duplicate or not decision or not decision.relevant):
            return "ignored"
        now = iso(utcnow())
        classification = decision.as_dict() if decision else json.loads(previous["classification_json"])
        active = not issue.duplicate and classification["relevant"]
        reason = "duplicate" if issue.duplicate else (None if active else "not_relevant")
        event = "created" if previous is None else "updated"
        if previous and previous["active"] and not active:
            event = "removed"
        elif previous and not previous["active"] and active:
            event = "restored"
        with self.conn:
            self.conn.execute("""INSERT INTO bugs (
                key,project,summary,category,active,resolution,status,duplicate_of,removed_reason,
                source_updated,first_seen,last_checked,last_attempt,last_error,classification_json,issue_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
                ON CONFLICT(key) DO UPDATE SET summary=excluded.summary,category=excluded.category,
                active=excluded.active,resolution=excluded.resolution,status=excluded.status,
                duplicate_of=excluded.duplicate_of,removed_reason=excluded.removed_reason,
                source_updated=excluded.source_updated,last_checked=excluded.last_checked,
                last_attempt=excluded.last_attempt,last_error=NULL,
                classification_json=excluded.classification_json,issue_json=excluded.issue_json""", (
                issue.key, issue.key.split("-")[0], issue.summary, classification["category"], int(active),
                issue.resolution, issue.status, issue.duplicate_of, reason, issue.updated_date,
                now, now, now, encode(classification), encode(issue.snapshot()),
            ))
            if previous is None or any(previous[k] != value for k, value in (
                ("active", int(active)), ("resolution", issue.resolution), ("source_updated", issue.updated_date),
                ("category", classification["category"]), ("duplicate_of", issue.duplicate_of),
            )):
                self.conn.execute("INSERT INTO events(key,at,event,details_json) VALUES(?,?,?,?)",
                    (issue.key, now, event, encode({"resolution": issue.resolution, "reason": reason, "duplicate_of": issue.duplicate_of})))
        return event

    def mark_error(self, key, error):
        with self.conn:
            self.conn.execute("UPDATE bugs SET last_attempt=?,last_error=? WHERE key=?", (iso(utcnow()), str(error), key))

    def due(self, projects, hours, duplicate_hours, limit, *, force=False):
        where = "project IN (" + ",".join("?" for _ in projects) + ")"
        params = list(projects)
        if not force:
            where += " AND ((removed_reason='duplicate' AND last_attempt<=?) OR ((removed_reason IS NULL OR removed_reason!='duplicate') AND last_attempt<=?))"
            params += [iso(utcnow() - timedelta(hours=duplicate_hours)), iso(utcnow() - timedelta(hours=hours))]
        params.append(limit)
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM bugs WHERE {where} ORDER BY last_attempt,key LIMIT ?", params)]

    def rows(self, *, include_removed=False, limit=None):
        sql = "SELECT * FROM bugs" + ("" if include_removed else " WHERE active=1") + " ORDER BY source_updated DESC,key"
        args = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (limit,)
        for row in self.conn.execute(sql, args):
            result = dict(row)
            result["active"] = bool(result["active"])
            result["classification"] = json.loads(result.pop("classification_json"))
            result["issue"] = json.loads(result.pop("issue_json"))
            yield result

    def start_run(self, kind):
        with self.conn:
            return self.conn.execute("INSERT INTO runs(kind,started_at) VALUES (?,?)", (kind, iso(utcnow()))).lastrowid

    def finish_run(self, run_id, result):
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at=?,result_json=? WHERE id=?", (iso(utcnow()), encode(result), run_id))

    def stats(self):
        counts = dict(self.conn.execute("SELECT category,COUNT(*) FROM bugs WHERE active=1 GROUP BY category"))
        runs = [dict(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5")]
        for run in runs:
            run["result"] = json.loads(run.pop("result_json") or "null")
        return {"active_by_category": counts,
                "active": sum(counts.values()),
                "removed": self.conn.execute("SELECT COUNT(*) FROM bugs WHERE active=0").fetchone()[0],
                "checkpoints": dict(self.conn.execute("SELECT * FROM metadata")),
                "recent_runs": runs}
