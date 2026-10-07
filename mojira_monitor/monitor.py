from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from .client import SourceError
from .classifier import Decision
from .models import iso, parse_time, utcnow

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, config, source, db, classifier):
        self.config, self.source, self.db, self.classifier = config, source, db, classifier
        self.namespace = hashlib.sha256(source.identity.encode()).hexdigest()[:12]

    def checkpoint_key(self, project):
        return f"checkpoint:{self.namespace}:{project}"

    def process(self, issue, *, force=False, reclassify=False):
        old = self.db.get_bug(issue.key)
        if old and parse_time(issue.updated_date) < parse_time(old["source_updated"]):
            raise SourceError("数据源返回比本地更旧的报告，保留现有状态")
        if not force and self.db.seen(issue, self.source.identity, self.classifier.version):
            return "unchanged"
        # New duplicate reports need no body/classification. Existing tracked ones
        # need a full snapshot to retain source links and information.
        if issue.duplicate and old is None:
            self.db.mark_seen(issue, self.source.identity, self.classifier.version)
            return "ignored_duplicate"
        if not issue.full:
            issue = self.source.issue(issue.key)
        if old and parse_time(issue.updated_date) < parse_time(old["source_updated"]):
            raise SourceError("详情数据源落后，保留现有状态")
        if issue.duplicate:
            decision = None
        elif old and not reclassify and self.db.seen(issue, self.source.identity, self.classifier.version):
            decision = Decision(**json.loads(old["classification_json"]))
        else:
            decision = self.classifier.classify(issue)
        action = self.db.save_bug(issue, decision)
        self.db.mark_seen(issue, self.source.identity, self.classifier.version)
        if action in {"created", "removed", "restored"}:
            log.info("%s %s %s", action, issue.key, issue.summary)
        return action

    def collect(self, *, backfill=False):
        cfg = self.config.polling
        started = utcnow()
        run = self.db.start_run("backfill" if backfill else "collect")
        result = {"projects": {}, "errors": 0, "processed": 0}
        for project in self.config.projects:
            checkpoint = self.db.get_meta(self.checkpoint_key(project))
            bootstrap = not checkpoint and not backfill
            since = (started - timedelta(hours=cfg.backfill_hours)) if backfill else (
                parse_time(checkpoint) - timedelta(hours=cfg.overlap_hours) if checkpoint
                else started - timedelta(hours=cfg.initial_hours))
            cap = cfg.bootstrap_pages if bootstrap else cfg.max_pages
            stats = {"pages": 0, "examined": 0, "actions": {}, "complete": False, "bootstrap_truncated": False, "errors": []}
            seen_keys = set()
            previous_oldest = None
            try:
                for page_number in range(1, cap + 1):
                    page = self.source.page(project, page_number, cfg.page_size)
                    stats["pages"] += 1
                    dates = [parse_time(i.updated_date) for i in page.issues]
                    if dates != sorted(dates, reverse=True):
                        raise SourceError("返回列表未按更新时间倒序，停止推进检查点")
                    keys = {i.key for i in page.issues}
                    if keys and keys <= seen_keys:
                        raise SourceError("分页重复，停止推进检查点")
                    # Concurrent changes can move items between offset pages. We
                    # deduplicate overlaps and recover omissions by overlap/backfill.
                    if dates and previous_oldest and dates[0] > previous_oldest:
                        log.warning("%s 分页期间发生更新，将通过重叠窗口回扫", project)
                    if dates:
                        previous_oldest = dates[-1]
                    for issue in page.issues:
                        if issue.key in seen_keys or parse_time(issue.updated_date) < since:
                            continue
                        seen_keys.add(issue.key)
                        stats["examined"] += 1
                        try:
                            action = self.process(issue)
                            stats["actions"][action] = stats["actions"].get(action, 0) + 1
                            result["processed"] += 1
                        except SourceError as exc:
                            stats["errors"].append(f"{issue.key}: {exc}")
                            self.db.mark_error(issue.key, exc)
                            log.warning("%s: %s", issue.key, exc)
                    if not page.has_more or (dates and min(dates) < since):
                        stats["complete"] = True
                        break
                if not stats["complete"]:
                    if bootstrap:
                        stats["bootstrap_truncated"] = True
                        log.info("%s 首次初始化达到 %s 页；按配置只采最近一部分", project, cap)
                    else:
                        raise SourceError("达到 max_pages 仍未覆盖检查点；请增大 max_pages，本轮不推进检查点")
                if not stats["errors"] and not backfill:
                    self.db.set_meta(self.checkpoint_key(project), iso(started))
            except SourceError as exc:
                stats["errors"].append(str(exc))
                log.error("%s: %s", project, exc)
            result["errors"] += len(stats["errors"])
            result["projects"][project] = stats
        if backfill and not result["errors"]:
            self.db.set_meta(f"last_backfill:{self.namespace}", iso(started))
        self.db.finish_run(run, result)
        return result

    def recheck(self, *, force=False):
        cfg = self.config.polling
        run = self.db.start_run("recheck")
        rows = self.db.due(self.config.projects, cfg.recheck_hours, cfg.duplicate_recheck_hours, cfg.recheck_batch, force=force)
        result = {"checked": 0, "errors": 0, "actions": {}, "failures": []}
        for row in rows:
            try:
                issue = self.source.issue(row["key"])
                action = self.process(issue, force=True, reclassify=force)
                result["actions"][action] = result["actions"].get(action, 0) + 1
                result["checked"] += 1
            except SourceError as exc:
                self.db.mark_error(row["key"], exc)
                result["errors"] += 1
                result["failures"].append({"key": row["key"], "error": str(exc)})
                log.warning("复查 %s: %s", row["key"], exc)
        self.db.finish_run(run, result)
        return result
