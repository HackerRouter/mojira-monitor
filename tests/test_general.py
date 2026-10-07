from dataclasses import replace
from datetime import timedelta
import json
import signal

import httpx
import pytest

from mojira_monitor import cli
from mojira_monitor.classifier import Classifier
from mojira_monitor.client import JsonHTTP, Page, SourceError
from mojira_monitor.config import Classification, Config, Filters, HTTP, load_configs, select_configs
from mojira_monitor.database import Database
from mojira_monitor.models import Issue, from_official, from_mirror, iso, utcnow
from mojira_monitor.monitor import Monitor


def report(**kwargs):
    return Issue(**{**dict(key="MC-10", summary="Redstone piston stops working", description="The piston stops moving.",
                          resolution="Unresolved", updated_date=iso(utcnow() - timedelta(minutes=1))), **kwargs})


def config(**kwargs):
    return Config(projects=["MC"], classification=Classification(mode="keywords", keywords_any=["piston"]), **kwargs)


@pytest.mark.parametrize("updates,summary,expected", [
    ({"keywords_any": ["piston", "红石"]}, "红石更新异常", True),
    ({"keywords_all": ["piston", "broken"]}, "Piston stuck", False),
    ({"keywords_all": ["piston", "broken"]}, "Piston is BROKEN", True),
    ({"keywords_any": [], "keywords_all": ["piston"]}, "Piston update", True),
    ({"keywords_any": ["not present"], "patterns_any": [r"\bpiston\b"]}, "A piston fails", True),
    ({"exclude_keywords": ["request"]}, "Piston feature request", False),
    ({"exclude_patterns": [r"\brequest\b"]}, "Piston feature request", False),
    ({"case_sensitive": True}, "PISTON fails", False),
    ({"keywords_any": ["c++"]}, "C++ client behavior", True),
])
def test_general_keywords(updates, summary, expected):
    cfg = config()
    for key, value in updates.items():
        setattr(cfg.classification, key, value)
    cfg.validate()
    decision = Classifier(cfg).classify(report(summary=summary, description=""))
    assert decision.relevant is expected
    if expected:
        assert decision.category == "matched"
        assert decision.evidence


def test_fields_only_search_selected_text():
    cfg = config()
    cfg.classification.fields = ["summary"]
    assert not Classifier(cfg).classify(report(summary="Blocks stop working", description="piston")).relevant
    cfg.classification.fields = ["description"]
    assert Classifier(cfg).classify(report(summary="Blocks stop working", description="piston")).relevant


@pytest.mark.parametrize("labels,expected", [
    (["redstone", "confirmed"], True),
    (["piston", "confirmed"], True),
    (["redstone"], False),
    (["redstone", "confirmed", "invalid"], False),
    (["REDSTONE", "confirmed"], False),
    ([], False),
])
def test_issue_labels_are_exact_and_combined(labels, expected):
    cfg = config(filters=Filters(labels_any=["redstone", "piston"], labels_all=["confirmed"], exclude_labels=["invalid"]))
    assert Classifier(cfg).classify(report(labels=labels)).relevant is expected


def test_label_only_monitor_and_project_filter():
    cfg = config(filters=Filters(labels_any=["regression"]))
    cfg.classification.keywords_any = []
    cfg.classification.category = "regressions"
    cfg.validate()
    result = Classifier(cfg).classify(report(summary="Texture shifted", labels=["regression"]))
    assert result.relevant and result.category == "regressions"
    assert result.evidence == ["label:regression"]
    assert not Classifier(cfg).classify(report(key="MCPE-10", labels=["regression"])).relevant


def test_all_mode_is_not_crash_specific():
    cfg = config()
    cfg.classification = Classification(mode="all", category="all_issues")
    cfg.validate()
    assert Classifier(cfg).classify(report(summary="Wrong texture", description="")).relevant
    cfg.classification.exclude_keywords = ["texture"]
    assert not Classifier(cfg).classify(report(summary="Wrong texture", description="")).relevant


def test_missing_or_malformed_labels_are_not_silently_empty():
    raw = {"key": "MC-10", "fields": {"summary": "Piston", "updated": iso(utcnow()), "resolution": None, "description": "Piston fails"}}
    with pytest.raises(KeyError):
        from_official(raw)
    raw["fields"]["labels"] = "redstone"
    with pytest.raises(ValueError):
        from_official(raw)
    mirror = {"key": "MC-10", "summary": "Piston", "updated_date": iso(utcnow()), "resolution": "Unresolved"}
    with pytest.raises(KeyError):
        from_mirror(mirror, full=True)
    assert from_mirror(mirror, full=False).labels == []


@pytest.mark.parametrize("updates", [
    {"keywords_any": "piston"}, {"keywords_any": [""]}, {"fields": []},
    {"fields": ["unknown"]}, {"patterns_any": ["["]}, {"mode": "unknown"},
    {"keywords_any": []},
])
def test_invalid_rules_fail_early(updates):
    cfg = config()
    for key, value in updates.items():
        setattr(cfg.classification, key, value)
    with pytest.raises(ValueError):
        cfg.validate()


def write_profiles(tmp_path, extra=""):
    path = tmp_path / "profiles.toml"
    path.write_text('''database="data/issues.db"
projects=["MC", "MCPE"]
[classification]
mode="keywords"
keywords_any=["piston"]
[polling]
collect_minutes=21
[[monitors]]
id="redstone"
name="红石"
tags=["mechanics"]
projects=["MC"]
[monitors.polling]
recheck_hours=5
[[monitors]]
id="bedrock"
enabled=false
projects=["MCPE"]
[monitors.classification]
keywords_any=["lag"]
''' + extra)
    return path


def test_profiles_inherit_sections_and_isolate_databases(tmp_path):
    configs = load_configs(write_profiles(tmp_path))
    a, b = configs
    assert a.projects == ["MC"] and b.projects == ["MCPE"]
    assert a.polling.collect_minutes == b.polling.collect_minutes == 21
    assert a.polling.recheck_hours == 5 and b.polling.recheck_hours == 12
    assert a.classification.keywords_any == ["piston"]
    assert b.classification.keywords_any == ["lag"]
    assert a.database == tmp_path / "data/issues.redstone.db"
    assert b.database == tmp_path / "data/issues.bedrock.db"
    assert select_configs(configs, None) == [a]
    assert select_configs(configs, ["bedrock"]) == [b]
    with pytest.raises(ValueError, match="未知"):
        select_configs(configs, ["typo"])


@pytest.mark.parametrize("second", ['id="one"', 'id="two"\ndatabase="data/same.db"'])
def test_duplicate_profile_id_or_database_rejected(tmp_path, second):
    path = tmp_path / "bad.toml"
    path.write_text('[[monitors]]\nid="one"\ndatabase="data/same.db"\n[[monitors]]\n' + second)
    with pytest.raises(ValueError):
        load_configs(path)


def test_legacy_database_adopted_without_losing_reports(tmp_path):
    db = Database(tmp_path / "legacy.db")
    original = report(summary="Client crashes")
    db.save_bug(original, Classifier(Config()).classify(original))
    db.set_meta("checkpoint:old:MC", "existing-checkpoint")
    db.bind_monitor("crashes")
    assert db.get_bug(original.key)["active"] == 1
    assert db.get_meta("checkpoint:old:MC") == "existing-checkpoint"
    with pytest.raises(ValueError, match="已属于"):
        db.bind_monitor("unrelated")
    assert db.get_meta("monitor_id") == "crashes"
    db.close()


class MemorySource:
    identity = "memory:official"

    def __init__(self, items):
        self.items = items

    def issue(self, key):
        return next(i for i in self.items if i.key == key)

    def page(self, project, page, limit):
        return Page([i for i in self.items if i.key.startswith(project + "-")], False)


def test_profiles_do_not_remove_each_others_matches(tmp_path):
    original = report(summary="Piston causes frame lag")
    source = MemorySource([original])
    cfg1 = config(monitor_id="redstone", database=tmp_path / "a.db")
    cfg2 = config(monitor_id="performance", database=tmp_path / "b.db")
    cfg2.classification.keywords_any = ["lag"]
    db1, db2 = Database(cfg1.database), Database(cfg2.database)
    try:
        a, b = Monitor(cfg1, source, db1, Classifier(cfg1)), Monitor(cfg2, source, db2, Classifier(cfg2))
        assert a.collect()["errors"] == b.collect()["errors"] == 0
        assert db1.stats()["active"] == db2.stats()["active"] == 1
        source.items = [replace(original, summary="Piston works incorrectly", updated_date=iso(utcnow()))]
        a.recheck(force=True)
        b.recheck(force=True)
        assert db1.stats()["active"] == 1 and db2.stats()["active"] == 0
        source.items = [replace(source.items[0], resolution="Duplicate", updated_date=iso(utcnow()))]
        a.recheck(force=True)
        assert db1.get_bug(original.key)["removed_reason"] == "duplicate"
        source.items = [replace(source.items[0], resolution="Unresolved", updated_date=iso(utcnow()))]
        a.recheck(force=True)
        assert db1.stats()["active"] == 1
    finally:
        db1.close()
        db2.close()


def test_changed_rule_invalidates_seen_cache(tmp_path):
    cfg = config(database=tmp_path / "rules.db")
    db = Database(cfg.database)
    original = report()
    source = MemorySource([original])
    try:
        Monitor(cfg, source, db, Classifier(cfg)).process(original)
        cfg.classification.keywords_any = ["unrelated"]
        Monitor(cfg, source, db, Classifier(cfg)).process(original)
        assert db.get_bug(original.key)["active"] == 0
    finally:
        db.close()


def test_general_ai_and_hard_exclusions(monkeypatch):
    cfg = config()
    cfg.classification.category = "redstone"
    cfg.classification.exclude_keywords = ["feature request"]
    cfg.ai.mode, cfg.ai.base_url, cfg.ai.model = "all", "https://example.test/v1", "test"
    cfg.ai.criteria = "Reports about redstone mechanisms"
    monkeypatch.setenv(cfg.ai.api_key_env, "fake-key")
    calls = []
    def handler(request):
        body = json.loads(request.content)
        assert "need not concern crashes" in body["messages"][0]["content"]
        assert cfg.ai.criteria in body["messages"][0]["content"]
        calls.append(body)
        content = json.dumps({"category": "matched", "confidence": 0.9, "evidence": ["Redstone"]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    http = JsonHTTP(HTTP(min_interval_seconds=0), client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        classifier = Classifier(cfg, http)
        decision = classifier.classify(report(summary="Redstone circuit behaves incorrectly", description=""))
        assert decision.relevant and decision.category == "redstone" and decision.method == "ai"
        assert not classifier.classify(report(summary="Redstone feature request")).relevant
        assert not classifier.classify(report(key="MCPE-10")).relevant
        assert len(calls) == 1
    finally:
        http.close()


def test_cli_profile_selection_export_and_no_secret_requirement(tmp_path, capsys):
    path = write_profiles(tmp_path)
    configs = load_configs(path)
    assert cli.main(["--config", str(path), "monitors"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    db = Database(configs[0].database)
    db.save_bug(report(), Classifier(configs[0]).classify(report()))
    db.close()
    assert cli.main(["--config", str(path), "--monitor", "redstone", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["monitor_id"] == "redstone" and rows[0]["monitor_tags"] == ["mechanics"]
    output = tmp_path / "export.json"
    assert cli.main(["--config", str(path), "export", "--output", str(output)]) == 0
    capsys.readouterr()
    assert json.loads(output.read_text())[0]["key"] == "MC-10"
    assert not configs[1].database.exists()


def test_cli_runs_selected_monitors_even_if_disabled(tmp_path, monkeypatch, capsys):
    path = write_profiles(tmp_path)
    items = [report(), report(key="MCPE-10", summary="Frame lag on Bedrock", description="")]
    monkeypatch.setattr(cli, "Source", lambda cfg, http: MemorySource(items))
    assert cli.main(["--config", str(path), "--monitor", "redstone", "--monitor", "bedrock", "once"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["errors"] == 0
    assert {r["monitor_id"] for r in result["monitors"]} == {"redstone", "bedrock"}
    for cfg in load_configs(path):
        db = Database(cfg.database)
        assert db.stats()["active"] == 1
        db.close()


def test_multi_monitor_scheduler_independent_intervals(tmp_path, monkeypatch):
    now = [0.0]
    collect = {"fast": [], "slow": []}
    class StopEvent:
        def is_set(self):
            return now[0] >= 121
        def set(self):
            now[0] = 1000
        def wait(self, seconds):
            now[0] += seconds
    monkeypatch.setattr(cli.threading, "Event", StopEvent)
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cli.signal, "signal", lambda sig, handler: signal.SIG_DFL)
    monkeypatch.setattr(cli, "emit", lambda _: None)
    dbs = []
    monitors = []
    try:
        for monitor_id, minutes in (("fast", 1), ("slow", 2)):
            cfg = config(monitor_id=monitor_id, database=tmp_path / f"{monitor_id}.db")
            cfg.polling.collect_minutes = minutes
            db = Database(cfg.database)
            dbs.append(db)
            monitor = Monitor(cfg, MemorySource([]), db, Classifier(cfg))
            def capture(*, backfill=False, monitor_id=monitor_id):
                collect[monitor_id].append(now[0])
                return {"errors": 0}
            monitor.collect = capture
            monitors.append(monitor)
        assert cli.daemon(monitors) == 0
        assert collect == {"fast": [0, 60, 120], "slow": [0, 120]}
    finally:
        for db in dbs:
            db.close()
