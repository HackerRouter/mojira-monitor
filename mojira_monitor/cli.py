from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import json
import logging
from pathlib import Path
import signal
import sys
import threading
import time

from .classifier import Classifier
from .client import JsonHTTP, Source, SourceError
from .config import load_configs, select_configs
from .database import Database
from .models import iso, parse_time, utcnow
from .monitor import Monitor

log = logging.getLogger(__name__)


@contextmanager
def writer_lock(database):
    path = Path(str(database) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another process is using this database; do not run the daemon and cron simultaneously") from exc
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def daemon(monitors):
    stopped = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    schedules = []
    for monitor in monitors:
        cfg = monitor.config.polling
        marker = f"last_backfill:{monitor.namespace}"
        last = monitor.db.get_meta(marker)
        if last is None:
            monitor.db.set_meta(marker, iso(utcnow()))
            last = monitor.db.get_meta(marker)
        backfill = time.monotonic() + max(0, cfg.backfill_interval_hours * 3600 - (utcnow() - parse_time(last)).total_seconds())
        schedules.append({"collect": 0.0, "recheck": 0.0, "backfill": backfill})
        log.info("Starting monitor %s: projects=%s source=%s database=%s", monitor.config.monitor_id,
                 monitor.config.projects, monitor.config.source, monitor.config.database)
    try:
        while not stopped.is_set():
            for monitor, schedule in zip(monitors, schedules):
                cfg = monitor.config.polling
                jobs = {"collect": (lambda: monitor.collect(), cfg.collect_minutes * 60),
                        "recheck": (lambda: monitor.recheck(), 60),
                        "backfill": (lambda: monitor.collect(backfill=True), cfg.backfill_interval_hours * 3600)}
                for kind, (job, interval) in jobs.items():
                    if not stopped.is_set() and time.monotonic() >= schedule[kind]:
                        emit({"monitor_id": monitor.config.monitor_id, kind: job()})
                        schedule[kind] = time.monotonic() + interval
            next_job = min(value for schedule in schedules for value in schedule.values())
            stopped.wait(max(0.1, min(30, next_job - time.monotonic())))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def parser():
    p = argparse.ArgumentParser(description="Monitor Mojira reports by project, keywords, and labels, and track duplicate resolutions")
    p.add_argument("--config", default="config.toml", help="Path to the configuration file")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--monitor", action="append", help="Select a monitor ID; repeat to select several (default: all enabled monitors)")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("monitors", help="List monitors and their effective configuration without secrets")
    commands.add_parser("init", help="Initialize the databases")
    commands.add_parser("doctor", help="Check source connectivity for each project (read-only)")
    commands.add_parser("collect", help="Collect recently created or updated reports once")
    commands.add_parser("backfill", help="Rescan the configured historical window")
    commands.add_parser("once", help="Collect and recheck once, suitable for cron")
    commands.add_parser("run", help="Continuously schedule collection, rechecks, and backfills")
    recheck = commands.add_parser("recheck", help="Recheck stored reports")
    recheck.add_argument("--force", action="store_true", help="Ignore recheck intervals while respecting the batch limit")
    listing = commands.add_parser("list", help="List active reports")
    listing.add_argument("--all", action="store_true", help="Include inactive reports")
    listing.add_argument("--limit", type=int, default=20, help="Maximum number of reports per monitor")
    commands.add_parser("status", help="Show counts, checkpoints, and recent run results")
    export = commands.add_parser("export", help="Export JSON to standard output or a file")
    export.add_argument("--all", action="store_true", help="Include inactive reports")
    export.add_argument("--output", type=Path)
    return p


def config_summary(config):
    return {"monitor_id": config.monitor_id, "name": config.name, "enabled": config.enabled,
            "tags": config.tags, "projects": config.projects, "source": config.source,
            "database": str(config.database), "classification": vars(config.classification),
            "filters": vars(config.filters), "polling": vars(config.polling), "ai_mode": config.ai.mode}


def tagged_rows(config, db, args):
    for row in db.rows(include_removed=args.all, limit=getattr(args, "limit", None)):
        yield {"monitor_id": config.monitor_id, "monitor_name": config.name, "monitor_tags": config.tags, **row}


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        all_configs = load_configs(args.config)
        if args.command == "monitors":
            selected = select_configs(all_configs, args.monitor) if args.monitor else all_configs
            emit([config_summary(c) for c in selected])
            return 0
        configs = select_configs(all_configs, args.monitor)
        if args.command == "list" and args.limit < 1:
            raise ValueError("limit must be greater than 0")
        with ExitStack() as stack:
            writing = args.command in {"init", "run", "once", "collect", "backfill", "recheck"}
            if writing:
                for config in sorted(configs, key=lambda c: str(c.database)):
                    stack.enter_context(writer_lock(config.database))
            if args.command == "doctor":
                checks = []
                for config in configs:
                    http = JsonHTTP(config.http)
                    stack.callback(http.close)
                    source = Source(config, http)
                    for project in config.projects:
                        check = {"monitor_id": config.monitor_id, "source": config.source, "project": project}
                        try:
                            page = source.page(project, 1, 1)
                            check.update(ok=True, sample_key=page.issues[0].key if page.issues else None)
                        except SourceError as exc:
                            check.update(ok=False, error=str(exc))
                        checks.append(check)
                emit({"checks": checks})
                return int(any(not r["ok"] for r in checks))
            databases = []
            for config in configs:
                db = Database(config.database)
                stack.callback(db.close)
                db.bind_monitor(config.monitor_id)
                databases.append(db)
            pairs = list(zip(configs, databases))
            if args.command == "init":
                emit([{**config_summary(c), "initialized": True} for c in configs])
                return 0
            if args.command == "status":
                emit([{ "monitor_id": c.monitor_id, "database": str(c.database), **db.stats()} for c, db in pairs])
                return 0
            if args.command in {"list", "export"}:
                rows = [row for c, db in pairs for row in tagged_rows(c, db, args)]
                output = getattr(args, "output", None)
                if output:
                    if output.resolve() in {c.database for c in all_configs}:
                        raise ValueError("The export file must not overwrite a database")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("x", encoding="utf-8") as f:
                        json.dump(rows, f, ensure_ascii=False, indent=2)
                        f.write("\n")
                    emit({"output": str(output), "count": len(rows)})
                else:
                    emit(rows)
                return 0
            monitors = []
            for config, db in pairs:
                http = JsonHTTP(config.http)
                stack.callback(http.close)
                ai_http = None
                if config.ai.mode != "off":
                    ai_http = JsonHTTP(config.http)
                    stack.callback(ai_http.close)
                monitors.append(Monitor(config, Source(config, http), db, Classifier(config, ai_http)))
            if args.command == "run":
                return daemon(monitors)
            results = []
            for monitor in monitors:
                if args.command in {"collect", "backfill"}:
                    result = monitor.collect(backfill=args.command == "backfill")
                elif args.command == "recheck":
                    result = monitor.recheck(force=args.force)
                else:
                    collect = monitor.collect()
                    recheck = monitor.recheck()
                    result = {"collect": collect, "recheck": recheck, "errors": collect["errors"] + recheck["errors"]}
                results.append({"monitor_id": monitor.config.monitor_id, **result})
            emit({"monitors": results, "errors": sum(r["errors"] for r in results)})
            return int(any(r["errors"] > 0 for r in results))
    except (OSError, ValueError, TypeError, SourceError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
