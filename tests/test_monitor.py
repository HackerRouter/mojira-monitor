from dataclasses import replace
from datetime import timedelta
import json

import httpx
import pytest

from mojira_monitor.classifier import Classifier
from mojira_monitor.client import JsonHTTP, NotFound, Page, Source, SourceError
from mojira_monitor.config import Config, HTTP, load_config
from mojira_monitor.database import Database
from mojira_monitor.models import Issue, from_official, iso, plain_text, utcnow
from mojira_monitor.monitor import Monitor


def issue(key="MC-1", **kwargs):
    data = dict(key=key, summary="Dedicated server crashes when loading chunks",
                updated_date=iso(utcnow() - timedelta(minutes=1)), resolution="Unresolved",
                description="The server process crashes while ticking an entity.")
    data.update(kwargs)
    return Issue(**data)


def official(i):
    return {"key": i.key, "fields": {
        "summary": i.summary, "updated": i.updated_date,
        "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": i.description}]}]},
        "resolution": None if i.resolution == "Unresolved" else {"name": i.resolution},
        "status": {"name": "Open"}, "versions": [{"name": "26.3"}], "issuelinks": [], "labels": i.labels,
    }}


class FakeSource:
    identity = "fake:official"

    def __init__(self):
        self.pages = {}
        self.details = {}
        self.calls = []

    def page(self, project, page, limit):
        self.calls.append((project, page))
        value = self.pages.get((project, page), Page([], False))
        if isinstance(value, Exception):
            raise value
        return value

    def issue(self, key):
        value = self.details[key]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def setup(tmp_path):
    cfg = Config(database=tmp_path / "bugs.db", projects=["MC"])
    cfg.polling.page_size = 2
    cfg.polling.bootstrap_pages = 2
    cfg.polling.max_pages = 2
    db = Database(cfg.database)
    source = FakeSource()
    monitor = Monitor(cfg, source, db, Classifier(cfg))
    yield cfg, db, source, monitor
    db.close()


def test_adf_encoded_and_plain_text():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Server "}, {"type": "text", "text": "crashes"}]},
        {"type": "codeBlock", "content": [{"type": "text", "text": "OutOfMemoryError"}]}]}
    assert plain_text(adf) == "Server crashes\nOutOfMemoryError\n"
    assert plain_text(json.dumps(adf)) == plain_text(adf)
    assert plain_text("{invalid plain text") == "{invalid plain text"
    assert "server crashes" in plain_text("<p>server crashes</p>")


@pytest.mark.parametrize("summary,category,relevant", [
    ("Dedicated server crashes on startup", "server_crash", True),
    ("Client crashes rendering chunks", "client_crash", True),
    ("Game crashes", "crash_unspecified", True),
    ("Server crashes", "server_crash", True),
    ("Client crashes", "client_crash", True),
    ("The game does not crash", "other", False),
    ("Without crashing, the server disconnects", "other", False),
    ("The game freezes", "hang", False),
    ("Disconnect from server", "other", False),
    ("Minecraft closes after launch", "crash_unspecified", True),
])
def test_keyword_classification(summary, category, relevant):
    result = Classifier(Config()).classify(issue(summary=summary, description=""))
    assert (result.category, result.relevant) == (category, relevant)


def test_hangs_opt_in_and_extra_rules():
    cfg = Config()
    cfg.classification.include_hangs = True
    cfg.classification.extra_patterns = [r"fatal engine failure"]
    assert Classifier(cfg).classify(issue(summary="Game hangs", description="")).relevant
    assert Classifier(cfg).classify(issue(summary="Fatal engine failure", description="")).relevant


def test_collect_dedup_duplicate_remove_restore(setup):
    cfg, db, source, monitor = setup
    original = issue()
    source.pages[("MC", 1)] = Page([original], False)
    assert monitor.collect()["errors"] == 0
    assert db.stats()["active"] == 1
    assert monitor.collect()["projects"]["MC"]["actions"] == {"unchanged": 1}
    dupe = replace(original, updated_date=iso(utcnow()), resolution="Duplicate",
                   links=[{"type": "duplicates", "key": "MC-99"}])
    source.details[original.key] = dupe
    assert monitor.recheck(force=True)["actions"] == {"removed": 1}
    assert list(db.rows()) == []
    assert db.get_bug("MC-1")["duplicate_of"] == "MC-99"
    source.pages[("MC", 1)] = Page([dupe], False)
    assert monitor.collect()["errors"] == 0
    assert db.stats()["active"] == 0
    source.details["MC-1"] = replace(dupe, updated_date=iso(utcnow()), resolution="Unresolved", links=[])
    assert monitor.recheck(force=True)["actions"] == {"restored": 1}
    assert db.stats()["active"] == 1
    assert db.get_bug("MC-1")["duplicate_of"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM bugs").fetchone()[0] == 1


@pytest.mark.parametrize("failure", [NotFound("404"), SourceError("timeout"), SourceError("HTTP 403")])
def test_unavailable_is_not_duplicate(setup, failure):
    cfg, db, source, monitor = setup
    monitor.process(issue())
    source.details["MC-1"] = failure
    result = monitor.recheck(force=True)
    assert result["errors"] == 1
    assert db.get_bug("MC-1")["active"] == 1
    assert db.get_bug("MC-1")["last_error"] == str(failure)


def test_stale_resolution_cannot_delete(setup):
    cfg, db, source, monitor = setup
    original = issue()
    monitor.process(original)
    source.details["MC-1"] = replace(original, resolution="Duplicate", updated_date=iso(utcnow() - timedelta(days=1)))
    assert monitor.recheck(force=True)["errors"] == 1
    assert db.get_bug("MC-1")["active"] == 1


def test_fixed_is_preserved(setup):
    cfg, db, source, monitor = setup
    monitor.process(issue())
    source.details["MC-1"] = issue(resolution="Fixed", fix_versions=["26.4"])
    assert monitor.recheck(force=True)["errors"] == 0
    assert db.get_bug("MC-1")["active"] == 1
    assert next(db.rows())["issue"]["fix_versions"] == ["26.4"]


def test_master_with_is_duplicated_by_is_not_removed(setup):
    cfg, db, source, monitor = setup
    monitor.process(issue(links=[{"type": "is duplicated by", "key": "MC-2"}]))
    assert db.get_bug("MC-1")["active"] == 1
    assert db.get_bug("MC-1")["duplicate_of"] is None


def test_new_duplicate_not_saved_as_bug(setup):
    cfg, db, source, monitor = setup
    assert monitor.process(issue(resolution="Duplicate")) == "ignored_duplicate"
    assert db.get_bug("MC-1") is None


def test_rejected_report_can_become_relevant_after_edit(setup):
    cfg, db, source, monitor = setup
    monitor.process(issue(summary="Texture misaligned", description=""))
    assert db.get_bug("MC-1") is None
    monitor.process(issue(summary="Client crashes on startup", updated_date=iso(utcnow())))
    assert db.get_bug("MC-1")["active"] == 1


def test_full_window_pagination_and_no_skipped_page(setup):
    cfg, db, source, monitor = setup
    db.set_meta(monitor.checkpoint_key("MC"), iso(utcnow() - timedelta(minutes=5)))
    same_time = iso(utcnow() - timedelta(minutes=1))
    source.pages[("MC", 1)] = Page([issue("MC-1", updated_date=same_time), issue("MC-2", updated_date=same_time)], True)
    source.pages[("MC", 2)] = Page([issue("MC-3", updated_date=iso(utcnow() - timedelta(minutes=2)))], False)
    assert monitor.collect()["errors"] == 0
    assert source.calls == [("MC", 1), ("MC", 2)]
    assert db.stats()["active"] == 3


@pytest.mark.parametrize("problem", ["cap", "repeat", "error", "unsorted"])
def test_bad_pagination_does_not_advance_checkpoint(setup, problem):
    cfg, db, source, monitor = setup
    checkpoint = iso(utcnow() - timedelta(minutes=5))
    db.set_meta(monitor.checkpoint_key("MC"), checkpoint)
    first = issue("MC-1")
    source.pages[("MC", 1)] = Page([first], True)
    if problem == "cap":
        source.pages[("MC", 2)] = Page([issue("MC-2")], True)
    elif problem == "repeat":
        source.pages[("MC", 2)] = Page([first], True)
    elif problem == "error":
        source.pages[("MC", 2)] = SourceError("HTTP 500")
    else:
        source.pages[("MC", 1)] = Page([issue("MC-2", updated_date=iso(utcnow() - timedelta(days=1))), first], False)
    assert monitor.collect()["errors"] > 0
    assert db.get_meta(monitor.checkpoint_key("MC")) == checkpoint


def test_item_failure_retried_without_checkpoint_advance(setup):
    cfg, db, source, monitor = setup
    candidate = issue(full=False)
    source.pages[("MC", 1)] = Page([candidate], False)
    source.details["MC-1"] = SourceError("temporary")
    assert monitor.collect()["errors"] == 1
    assert db.get_meta(monitor.checkpoint_key("MC")) is None
    source.details["MC-1"] = replace(candidate, full=True)
    assert monitor.collect()["errors"] == 0
    assert db.get_meta(monitor.checkpoint_key("MC")) is not None


def test_bootstrap_is_explicitly_bounded(setup):
    cfg, db, source, monitor = setup
    cfg.polling.bootstrap_pages = 1
    source.pages[("MC", 1)] = Page([issue()], True)
    result = monitor.collect()
    assert result["errors"] == 0
    assert result["projects"]["MC"]["bootstrap_truncated"] is True
    assert db.get_meta(monitor.checkpoint_key("MC"))


def test_backfill_does_not_change_incremental_checkpoint(setup):
    cfg, db, source, monitor = setup
    checkpoint = iso(utcnow())
    db.set_meta(monitor.checkpoint_key("MC"), checkpoint)
    source.pages[("MC", 1)] = Page([issue(updated_date=iso(utcnow() - timedelta(days=3)))], False)
    assert monitor.collect(backfill=True)["errors"] == 0
    assert db.stats()["active"] == 1
    assert db.get_meta(monitor.checkpoint_key("MC")) == checkpoint


def test_recheck_due_and_project_scope(setup):
    cfg, db, source, monitor = setup
    monitor.process(issue())
    monitor.process(issue("MCPE-2"))
    assert db.due(["MC"], 12, 168, 100) == []
    assert [r["key"] for r in db.due(["MC"], 12, 168, 100, force=True)] == ["MC-1"]


def test_unchanged_periodic_recheck_reuses_classification(setup, monkeypatch):
    cfg, db, source, monitor = setup
    original = issue()
    monitor.process(original)
    source.details["MC-1"] = original
    with db.conn:
        db.conn.execute("UPDATE bugs SET last_attempt=?", (iso(utcnow() - timedelta(days=2)),))
    def should_not_call(_):
        pytest.fail("unchanged report should reuse its classification")
    monkeypatch.setattr(monitor.classifier, "classify", should_not_call)
    assert monitor.recheck()["checked"] == 1
    assert db.get_bug("MC-1")["last_attempt"] > iso(utcnow() - timedelta(minutes=1))


def http_client(handler, retries=0, sleeps=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return JsonHTTP(HTTP(min_interval_seconds=0, retries=retries), client=client,
                    sleeper=(sleeps.append if sleeps is not None else lambda _: None))


def test_official_zero_based_page_parameter_and_normalization():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(201, json={"issues": [official(issue())], "pagination": {"currentPage": body["page"], "hasNextPage": False}})
    client = http_client(handler)
    source = Source(Config(), client)
    page = source.page("MC", 2, 50)
    assert requests[0]["page"] == 1
    assert "nextPageToken" not in requests[0]
    assert page.issues[0].description.startswith("The server")
    assert page.issues[0].affected_versions == ["26.3"]
    client.close()


def test_official_link_direction_and_missing_resolution():
    data = official(issue(resolution="Duplicate"))
    data["fields"]["issuelinks"] = [{"type": {"outward": "duplicates", "inward": "is duplicated by"}, "outwardIssue": {"key": "MC-99"}}]
    assert from_official(data).duplicate_of == "MC-99"
    del data["fields"]["resolution"]
    with pytest.raises(KeyError):
        from_official(data)


def test_mirror_list_and_detail():
    requests = []
    def handler(request):
        requests.append(request)
        item = {"key": "MC-1", "summary": "Client crashes", "updated_date": iso(utcnow()), "resolution": "Unresolved", "labels": []}
        if request.url.path.endswith("MC-1"):
            return httpx.Response(200, json=dict(item, description='{"type":"doc","content":[{"type":"text","text":"crash"}]}'))
        return httpx.Response(200, json={"issues": [item], "total_count": 1})
    cfg = Config(source="mirror")
    client = http_client(handler)
    source = Source(cfg, client)
    assert not source.page("MC", 1, 50).issues[0].full
    assert requests[0].url.params["sort"] == "updated"
    assert source.issue("MC-1").description == "crash"
    client.close()


def test_429_retry_after_and_500_retry():
    codes = iter([429, 503, 200])
    sleeps = []
    def handler(request):
        return httpx.Response(next(codes), headers={"retry-after": "3"}, json={"ok": True})
    client = http_client(handler, retries=2, sleeps=sleeps)
    assert client.request("GET", "https://example.test")["ok"]
    assert sleeps == [3, 3]
    client.close()


def test_long_retry_after_is_not_shortened():
    sleeps = []
    client = http_client(lambda _: httpx.Response(429, headers={"retry-after": "120"}), retries=3, sleeps=sleeps)
    with pytest.raises(SourceError, match="429"):
        client.request("GET", "https://example.test")
    assert not sleeps
    with pytest.raises(SourceError, match="cooldown"):
        client.request("GET", "https://example.test/another-project")
    client.close()


@pytest.mark.parametrize("response,match", [
    (httpx.Response(403, headers={"cf-mitigated": "challenge"}), "Cloudflare"),
    (httpx.Response(200, text="<html>challenge</html>"), "non-JSON"),
    (httpx.Response(404), "404"),
])
def test_http_failures_are_explicit(response, match):
    client = http_client(lambda _: response)
    with pytest.raises(SourceError, match=match):
        client.request("GET", "https://example.test")
    client.close()


def test_ai_grounded_classification_and_untrusted_input(monkeypatch):
    cfg = Config()
    cfg.ai.mode, cfg.ai.base_url, cfg.ai.model = "all", "https://example.test/v1", "test"
    monkeypatch.setenv(cfg.ai.api_key_env, "test-key")
    def handler(request):
        body = json.loads(request.content)
        assert "untrusted" in body["messages"][0]["content"]
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "category": "server_crash", "confidence": 0.9, "evidence": ["Dedicated server crashes"]})}}]})
    client = http_client(handler)
    result = Classifier(cfg, client).classify(issue())
    assert result.relevant and result.method == "ai"
    client.close()


def test_ai_hallucinated_quote_not_cached_as_negative(monkeypatch, setup):
    cfg, db, source, monitor = setup
    cfg.ai.mode, cfg.ai.base_url, cfg.ai.model = "all", "https://example.test/v1", "test"
    monkeypatch.setenv(cfg.ai.api_key_env, "test-key")
    client = http_client(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
        "category": "client_crash", "confidence": 0.9, "evidence": ["invented quote"]})}}]}))
    monitor.classifier = Classifier(cfg, client)
    candidate = issue()
    source.pages[("MC", 1)] = Page([candidate], False)
    assert monitor.collect()["errors"] == 1
    assert not db.seen(candidate, source.identity, monitor.classifier.version)
    assert db.get_meta(monitor.checkpoint_key("MC")) is None
    client.close()


def test_config_paths_relative_to_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('database = "data/test.db"\nprojects=["MC"]\n')
    loaded = load_config(config)
    assert loaded.database == tmp_path / "data/test.db"
    assert loaded.ai.mode == "off"


def test_invalid_project_cannot_inject_jql():
    with pytest.raises(ValueError):
        Config(projects=['MC OR project=WEB']).validate()
