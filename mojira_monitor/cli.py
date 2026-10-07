from __future__ import annotations

import argparse
from contextlib import contextmanager
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
from .config import load_config
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
            raise ValueError("已有采集进程操作此数据库；请勿同时启动 run 和 cron") from exc
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def daemon(monitor):
    stopped = threading.Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    cfg = monitor.config.polling
    next_collect = next_review = 0.0
    marker = f"last_backfill:{monitor.namespace}"
    last = monitor.db.get_meta(marker)
    # The first process start initializes the recent subset; the first full
    # lookback happens after the configured interval, or explicitly via backfill.
    if last is None:
        monitor.db.set_meta(marker, iso(utcnow()))
        last = monitor.db.get_meta(marker)
    next_backfill = time.monotonic() + max(0, cfg.backfill_interval_hours * 3600 - (utcnow() - parse_time(last)).total_seconds())
    log.info("监控启动：projects=%s source=%s database=%s", monitor.config.projects, monitor.config.source, monitor.config.database)
    try:
        while not stopped.is_set():
            if time.monotonic() >= next_collect:
                emit({"collect": monitor.collect()})
                next_collect = time.monotonic() + cfg.collect_minutes * 60
            if not stopped.is_set() and time.monotonic() >= next_review:
                emit({"recheck": monitor.recheck()})
                next_review = time.monotonic() + 60
            if not stopped.is_set() and time.monotonic() >= next_backfill:
                emit({"backfill": monitor.collect(backfill=True)})
                next_backfill = time.monotonic() + cfg.backfill_interval_hours * 3600
            stopped.wait(max(0.1, min(30, next_collect - time.monotonic(), next_review - time.monotonic(), next_backfill - time.monotonic())))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def parser():
    p = argparse.ArgumentParser(description="采集 Minecraft 崩溃报告并跟踪 Duplicate 状态")
    p.add_argument("--config", default="config.toml", help="配置文件路径")
    p.add_argument("--verbose", action="store_true")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化数据库")
    commands.add_parser("doctor", help="检查每个项目的数据源连通性（只读）")
    commands.add_parser("collect", help="采集一次最近新增/更新报告")
    commands.add_parser("backfill", help="回扫配置中的历史时间窗口")
    commands.add_parser("once", help="采集并复查一次，可交给 cron 调度")
    commands.add_parser("run", help="持续运行定时采集、复查和回扫")
    recheck = commands.add_parser("recheck", help="复查数据库中的报告")
    recheck.add_argument("--force", action="store_true", help="忽略复查时间，但仍遵循 batch 限制")
    listing = commands.add_parser("list", help="查看有效报告")
    listing.add_argument("--all", action="store_true", help="包括软删除记录")
    listing.add_argument("--limit", type=int, default=20)
    commands.add_parser("status", help="查看数量、检查点和最近任务结果")
    export = commands.add_parser("export", help="导出 JSON 到标准输出或指定文件")
    export.add_argument("--all", action="store_true", help="包括软删除记录")
    export.add_argument("--output", type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP debug logging may expose authentication details, including AI keys.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    db = http = ai_http = None
    try:
        config = load_config(args.config)
        if args.command == "list" and args.limit < 1:
            raise ValueError("limit 必须大于 0")
        db = Database(config.database)
        if args.command == "init":
            emit({"database": str(config.database), "initialized": True})
            return 0
        if args.command == "status":
            emit(db.stats())
            return 0
        if args.command == "list":
            emit(list(db.rows(include_removed=args.all, limit=args.limit)))
            return 0
        if args.command == "export":
            rows = list(db.rows(include_removed=args.all))
            if args.output:
                if args.output.resolve() == config.database:
                    raise ValueError("导出文件不可覆盖数据库")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation prevents overwriting unrelated files.
                with args.output.open("x", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                emit({"output": str(args.output), "count": len(rows)})
            else:
                emit(rows)
            return 0
        http = JsonHTTP(config.http)
        source = Source(config, http)
        if args.command == "doctor":
            results = []
            for project in config.projects:
                try:
                    page = source.page(project, 1, 1)
                    results.append({"project": project, "ok": True, "sample_key": page.issues[0].key if page.issues else None})
                except SourceError as exc:
                    results.append({"project": project, "ok": False, "error": str(exc)})
            emit({"source": config.source, "checks": results})
            return int(any(not r["ok"] for r in results))
        if config.ai.mode != "off":
            ai_http = JsonHTTP(config.http)
        classifier = Classifier(config, ai_http)
        monitor = Monitor(config, source, db, classifier)
        with writer_lock(config.database):
            if args.command == "run":
                return daemon(monitor)
            if args.command in {"collect", "backfill"}:
                result = monitor.collect(backfill=args.command == "backfill")
            elif args.command == "recheck":
                result = monitor.recheck(force=args.force)
            else:
                collect = monitor.collect()
                recheck = monitor.recheck()
                result = {"collect": collect, "recheck": recheck, "errors": collect["errors"] + recheck["errors"]}
            emit(result)
            return int(result["errors"] > 0)
    except (OSError, ValueError, TypeError, SourceError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        for resource in (ai_http, http, db):
            if resource:
                resource.close()


if __name__ == "__main__":
    sys.exit(main())
