from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Polling:
    collect_minutes: float = 20
    recheck_hours: float = 12
    duplicate_recheck_hours: float = 168
    recheck_batch: int = 100
    page_size: int = 50
    bootstrap_pages: int = 2
    initial_hours: float = 24
    overlap_hours: float = 6
    max_pages: int = 30
    backfill_hours: float = 168
    backfill_interval_hours: float = 24


@dataclass
class HTTP:
    timeout_seconds: float = 30
    min_interval_seconds: float = 1
    retries: int = 3


@dataclass
class Classification:
    include_hangs: bool = False
    extra_patterns: list[str] = field(default_factory=list)


@dataclass
class AI:
    mode: str = "off"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "MOJIRA_AI_API_KEY"
    min_confidence: float = 0.75
    max_chars: int = 16000


@dataclass
class Config:
    database: Path = Path("data/mojira.sqlite3")
    projects: list[str] = field(default_factory=lambda: ["MC", "MCPE", "BDS"])
    source: str = "official"
    official_url: str = "https://bugs.mojang.com/api/jql-search-post"
    mirror_url: str = "https://mojira.dev/api/v1"
    polling: Polling = field(default_factory=Polling)
    http: HTTP = field(default_factory=HTTP)
    classification: Classification = field(default_factory=Classification)
    ai: AI = field(default_factory=AI)

    def validate(self):
        if self.source not in {"official", "mirror"}:
            raise ValueError("source 必须为 official 或 mirror")
        if not self.projects or any(not re.fullmatch(r"[A-Z][A-Z0-9]*", p) for p in self.projects):
            raise ValueError("projects 必须为非空的项目代码列表，如 MC、MCPE、BDS")
        if len(set(self.projects)) != len(self.projects):
            raise ValueError("projects 不可重复")
        for name, value in vars(self.polling).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"polling.{name} 必须大于 0")
        for name in ("page_size", "bootstrap_pages", "max_pages", "recheck_batch"):
            if not isinstance(getattr(self.polling, name), int):
                raise ValueError(f"polling.{name} 必须为整数")
        if self.polling.page_size > 100:
            raise ValueError("page_size 最大为 100")
        if self.http.timeout_seconds <= 0 or self.http.min_interval_seconds < 0:
            raise ValueError("HTTP 超时必须大于 0，请求间隔不可小于 0")
        if not isinstance(self.http.retries, int) or not 0 <= self.http.retries <= 8:
            raise ValueError("http.retries 必须为 0 到 8 的整数")
        for pattern in self.classification.extra_patterns:
            re.compile(pattern)
        if self.ai.mode not in {"off", "candidates", "all"}:
            raise ValueError("ai.mode 必须为 off/candidates/all")
        if self.ai.mode != "off" and (not self.ai.base_url or not self.ai.model):
            raise ValueError("启用 AI 时必须设置 base_url 和 model")
        if not 0 <= self.ai.min_confidence <= 1 or self.ai.max_chars < 100:
            raise ValueError("AI 置信度需在 0～1 之间，max_chars 至少为 100")

    def classifier_version(self) -> str:
        data = {"rules_version": 1, "classification": vars(self.classification), "ai": vars(self.ai)}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open("rb") as f:
        data = tomllib.load(f)
    sections = {name: cls(**data.pop(name, {})) for name, cls in (
        ("polling", Polling), ("http", HTTP), ("classification", Classification), ("ai", AI)
    )}
    config = Config(**data, **sections)
    config.database = (path.parent / config.database).resolve()
    config.validate()
    return config
