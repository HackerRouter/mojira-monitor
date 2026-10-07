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
            headers={"User-Agent": "MojiraCrashMonitor/0.1", "Accept": "application/json"},
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
                raise SourceError("数据源要求稍后重试，仍处于 Retry-After 冷却期")
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
                raise SourceError(f"请求失败 ({type(exc).__name__})") from exc
            if response.status_code == 404:
                raise NotFound("HTTP 404：报告暂不可访问，保留本地记录")
            if response.status_code == 403:
                challenge = response.headers.get("cf-mitigated") == "challenge"
                hint = "Cloudflare 验证阻止访问；使用 official 数据源或可访问的自托管镜像" if challenge else "数据源拒绝访问"
                raise SourceError(f"HTTP 403：{hint}")
            if response.status_code == 429 or response.status_code >= 500:
                delay = max(self.retry_after(response.headers.get("retry-after")), min(2 ** attempt, 30))
                # Long server cooldowns are respected by failing this run, not shortened.
                if attempt < self.config.retries and delay <= 60:
                    self.sleep(delay)
                    continue
                self.not_before = self.clock() + delay
                raise SourceError(f"HTTP {response.status_code}：本轮停止，请稍后重试")
            if not 200 <= response.status_code < 300:
                raise SourceError(f"HTTP {response.status_code}：请求未成功")
            try:
                data = response.json()
            except ValueError as exc:
                raise SourceError("接口返回非 JSON 内容") from exc
            if not isinstance(data, dict):
                raise SourceError("接口 JSON 不是对象")
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
        """Internal page numbers are 1-based; official API uses 0-based pages."""
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
                    raise SourceError("官方接口未返回请求的页码，停止推进检查点")
                more = pagination.get("hasNextPage")
                if more is None and "isLast" in data:
                    more = not data["isLast"]
                if not isinstance(more, bool):
                    raise SourceError("官方接口缺少分页结束标志")
            else:
                data = self.http.request("GET", self.config.mirror_url.rstrip("/") + "/issues", params={
                    "project": project, "sort": "updated", "sort_dir": "DESC", "page": page, "limit": limit,
                })
                items = [from_mirror(item, full=False) for item in data["issues"]]
                more = page * limit < int(data["total_count"])
            if any(not i.key.startswith(project + "-") for i in items):
                raise SourceError("接口返回了其他项目的数据")
            if more and not items:
                raise SourceError("接口声称有下一页但当前页为空")
            return Page(items, more)
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"列表接口结构异常 ({type(exc).__name__})") from exc

    def issue(self, key: str) -> Issue:
        try:
            if self.config.source == "official":
                data = self.http.request("POST", self.config.official_url, json={
                    "advanced": True, "project": key.split("-")[0], "search": f"key = {key}",
                    "maxResults": 1, "page": 0,
                })
                if not isinstance(data.get("issues"), list):
                    raise SourceError("详情接口缺少 issues")
                if not data["issues"]:
                    raise NotFound("报告暂不可访问，保留本地记录")
                issue = from_official(data["issues"][0])
            else:
                data = self.http.request("GET", self.config.mirror_url.rstrip("/") + f"/issues/{key}")
                issue = from_mirror(data, full=True)
            if issue.key != key or not issue.full:
                raise SourceError("详情接口返回不完整或不匹配的 issue")
            return issue
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"详情接口结构异常 ({type(exc).__name__})") from exc
