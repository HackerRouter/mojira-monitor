from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .config import Config, HTTP
from .models import Issue, from_mirror, from_official


class SourceError(RuntimeError):
    pass


class NotFound(SourceError):
    pass


class JsonHTTP:
    def __init__(self, config: HTTP, *, client=None, sleeper=time.sleep, clock=time.monotonic):
        self.config = config
        self.client = client or httpx.Client(
            timeout=config.timeout_seconds, follow_redirects=False,
            headers={"User-Agent": "MojiraMonitor/0.2", "Accept": "application/json"},
        )
        self.sleep = sleeper
        self.clock = clock
        self.last_request = None
        self.not_before = 0.0

    def close(self):
        self.client.close()

    @staticmethod
    def retry_after(value):
        if not value:
            return 0
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                return 0

    def request(self, method, url, **kwargs):
        for attempt in range(self.config.retries + 1):
            cooldown = self.not_before - self.clock()
            if cooldown > 60:
                raise SourceError("The source is still in its Retry-After cooldown period")
            if cooldown > 0:
                self.sleep(cooldown)
                self.not_before = 0.0
            if self.last_request is not None:
                remaining = self.config.min_interval_seconds - (self.clock() - self.last_request)
                if remaining > 0:
                    self.sleep(remaining)
            self.last_request = self.clock()
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt < self.config.retries:
                    self.sleep(min(2 ** attempt, 30))
                    continue
                raise SourceError(f"Request failed ({type(exc).__name__})") from exc
            if response.status_code == 404:
                raise NotFound("HTTP 404: report temporarily unavailable; keeping the local record")
            if response.status_code == 403:
                challenge = response.headers.get("cf-mitigated") == "challenge"
                hint = "Access blocked by a Cloudflare challenge; use the official source or an accessible self-hosted mirror" if challenge else "Access denied by the source"
                raise SourceError(f"HTTP 403: {hint}")
            if response.status_code == 429 or response.status_code >= 500:
                delay = max(self.retry_after(response.headers.get("retry-after")), min(2 ** attempt, 30))
                if attempt < self.config.retries and delay <= 60:
                    self.sleep(delay)
                    continue
                self.not_before = self.clock() + delay
                raise SourceError(f"HTTP {response.status_code}: stopping this request; retry later")
            if not 200 <= response.status_code < 300:
                raise SourceError(f"HTTP {response.status_code}: request failed")
            try:
                data = response.json()
            except ValueError as exc:
                raise SourceError("The API returned non-JSON content") from exc
            if not isinstance(data, dict):
                raise SourceError("The API JSON response is not an object")
            return data
        raise AssertionError("unreachable")


@dataclass
class Page:
    issues: list[Issue]
    has_more: bool


class Source:
    def __init__(self, config: Config, http: JsonHTTP):
        self.config = config
        self.http = http
        url = config.official_url if config.source == "official" else config.mirror_url
        self.identity = f"{config.source}:{url}"

    def page(self, project: str, page: int, limit: int) -> Page:
        try:
            if self.config.source == "official":
                data = self.http.request("POST", self.config.official_url, json={
                    "advanced": True, "project": project,
                    "search": f"project = {project} ORDER BY updated DESC, key DESC",
                    "maxResults": limit, "page": page - 1,
                })
                items = [from_official(item) for item in data["issues"]]
                pagination = data.get("pagination", {})
                if "currentPage" in pagination and pagination["currentPage"] != page - 1:
                    raise SourceError("The official API returned the wrong page; checkpoint will not advance")
                more = pagination.get("hasNextPage")
                if more is None and "isLast" in data:
                    more = not data["isLast"]
                if not isinstance(more, bool):
                    raise SourceError("The official API response is missing its pagination completion flag")
            else:
                data = self.http.request("GET", self.config.mirror_url.rstrip("/") + "/issues", params={
                    "project": project, "sort": "updated", "sort_dir": "DESC", "page": page, "limit": limit,
                })
                items = [from_mirror(item, full=False) for item in data["issues"]]
                more = page * limit < int(data["total_count"])
            if any(not i.key.startswith(project + "-") for i in items):
                raise SourceError("The API returned issues from a different project")
            if more and not items:
                raise SourceError("The API indicates another page exists but returned an empty page")
            return Page(items, more)
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"Invalid issue-list response structure ({type(exc).__name__})") from exc

    def issue(self, key: str) -> Issue:
        try:
            if self.config.source == "official":
                data = self.http.request("POST", self.config.official_url, json={
                    "advanced": True, "project": key.split("-")[0], "search": f"key = {key}",
                    "maxResults": 1, "page": 0,
                })
                if not isinstance(data.get("issues"), list):
                    raise SourceError("The issue-detail response is missing issues")
                if not data["issues"]:
                    raise NotFound("Report temporarily unavailable; keeping the local record")
                issue = from_official(data["issues"][0])
            else:
                data = self.http.request("GET", self.config.mirror_url.rstrip("/") + f"/issues/{key}")
                issue = from_mirror(data, full=True)
            if issue.key != key or not issue.full:
                raise SourceError("The issue-detail response is incomplete or has a mismatched key")
            return issue
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"Invalid issue-detail response structure ({type(exc).__name__})") from exc
