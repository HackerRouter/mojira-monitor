from __future__ import annotations

import hashlib
import json
import math
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
    mode: str = "crash"  # crash / keywords / all
    category: str = "matched"
    fields: list[str] = field(default_factory=lambda: ["summary", "description", "environment"])
    case_sensitive: bool = False
    keywords_any: list[str] = field(default_factory=list)
    keywords_all: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    patterns_any: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    include_hangs: bool = False
    extra_patterns: list[str] = field(default_factory=list)


@dataclass
class Filters:
    labels_any: list[str] = field(default_factory=list)
    labels_all: list[str] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)


@dataclass
class AI:
    mode: str = "off"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "MOJIRA_AI_API_KEY"
    min_confidence: float = 0.75
    max_chars: int = 16000
    criteria: str = ""


@dataclass
class Config:
    monitor_id: str = "default"
    name: str = ""
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    database: Path = Path("data/mojira.sqlite3")
    projects: list[str] = field(default_factory=lambda: ["MC", "MCPE", "BDS"])
    source: str = "official"
    official_url: str = "https://bugs.mojang.com/api/jql-search-post"
    mirror_url: str = "https://mojira.dev/api/v1"
    polling: Polling = field(default_factory=Polling)
    http: HTTP = field(default_factory=HTTP)
    classification: Classification = field(default_factory=Classification)
    filters: Filters = field(default_factory=Filters)
    ai: AI = field(default_factory=AI)

    def validate(self):
        if not isinstance(self.monitor_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", self.monitor_id):
            raise ValueError("监控 id 只能包含字母、数字、下划线和短横线，最长 64 字符")
        if not isinstance(self.enabled, bool) or not isinstance(self.name, str):
            raise ValueError("enabled 必须为布尔值，name 必须为字符串")
        string_list(self.tags, "tags")
        if self.source not in {"official", "mirror"}:
            raise ValueError("source 必须为 official 或 mirror")
        string_list(self.projects, "projects")
        if not self.projects or any(not re.fullmatch(r"[A-Z][A-Z0-9]*", p) for p in self.projects):
            raise ValueError("projects 必须为非空的项目代码列表，如 MC、MCPE、BDS")
        if len(set(self.projects)) != len(self.projects):
            raise ValueError("projects 不可重复")
        for name, value in vars(self.polling).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"polling.{name} 必须大于 0")
        for name in ("page_size", "bootstrap_pages", "max_pages", "recheck_batch"):
            if not isinstance(getattr(self.polling, name), int):
                raise ValueError(f"polling.{name} 必须为整数")
        if self.polling.page_size > 100:
            raise ValueError("page_size 最大为 100")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in (self.http.timeout_seconds, self.http.min_interval_seconds)):
            raise ValueError("HTTP 超时和请求间隔必须是有限数值")
        if self.http.timeout_seconds <= 0 or self.http.min_interval_seconds < 0:
            raise ValueError("HTTP 超时必须大于 0，请求间隔不可小于 0")
        if not isinstance(self.http.retries, int) or not 0 <= self.http.retries <= 8:
            raise ValueError("http.retries 必须为 0 到 8 的整数")
        rules = self.classification
        if rules.mode not in {"crash", "keywords", "all"}:
            raise ValueError("classification.mode 必须为 crash/keywords/all")
        if not isinstance(rules.category, str) or not rules.category.strip():
            raise ValueError("classification.category 必须为非空字符串")
        if not isinstance(rules.case_sensitive, bool) or not isinstance(rules.include_hangs, bool):
            raise ValueError("case_sensitive 和 include_hangs 必须为布尔值")
        for name in ("fields", "keywords_any", "keywords_all", "exclude_keywords", "patterns_any", "exclude_patterns", "extra_patterns"):
            string_list(getattr(rules, name), f"classification.{name}")
        if not rules.fields or set(rules.fields) - {"summary", "description", "environment"}:
            raise ValueError("fields 可选择 summary、description、environment，且不可为空")
        for name, value in vars(self.filters).items():
            string_list(value, f"filters.{name}")
        for pattern in rules.extra_patterns + rules.patterns_any + rules.exclude_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"正则表达式无效: {exc}") from exc
        if rules.mode == "keywords" and not any((rules.keywords_any, rules.keywords_all, rules.patterns_any,
                                                  rules.extra_patterns, self.filters.labels_any, self.filters.labels_all)):
            raise ValueError("keywords 模式至少需要一项关键词、正则或正向标签条件；全部收集请使用 all 模式")
        if self.ai.mode not in {"off", "candidates", "all"}:
            raise ValueError("ai.mode 必须为 off/candidates/all")
        if self.ai.mode != "off" and (not self.ai.base_url or not self.ai.model):
            raise ValueError("启用 AI 时必须设置 base_url 和 model")
        if not 0 <= self.ai.min_confidence <= 1 or self.ai.max_chars < 100:
            raise ValueError("AI 置信度需在 0～1 之间，max_chars 至少为 100")
        if not isinstance(self.ai.criteria, str):
            raise ValueError("ai.criteria 必须为字符串")

    def classifier_version(self) -> str:
        data = {"rules_version": 2, "projects": self.projects, "filters": vars(self.filters),
                "classification": vars(self.classification), "ai": vars(self.ai)}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def string_list(value, name):
    if not isinstance(value, list) or any(not isinstance(s, str) or not s.strip() for s in value):
        raise ValueError(f"{name} 必须为非空字符串组成的数组")


SECTIONS = {"polling": Polling, "http": HTTP, "classification": Classification, "filters": Filters, "ai": AI}


def build_config(data: dict, path: Path) -> Config:
    data = dict(data)
    sections = {name: cls(**data.pop(name, {})) for name, cls in SECTIONS.items()}
    config = Config(**data, **sections)
    config.database = (path.parent / config.database).resolve()
    config.validate()
    return config


def load_configs(path: str | Path) -> list[Config]:
    """Load legacy single-monitor TOML or independently inherited [[monitors]]."""
    path = Path(path).resolve()
    with path.open("rb") as f:
        data = tomllib.load(f)
    profiles = data.pop("monitors", None)
    if profiles is None:
        return [build_config(data, path)]
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("monitors 必须是非空的 [[monitors]] 数组")
    configs = []
    for profile in profiles:
        if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
            raise ValueError("每个 [[monitors]] 必须设置字符串 id")
        own = dict(profile)
        monitor_id = own.pop("id")
        merged = {**data, **own, "monitor_id": monitor_id}
        for section in SECTIONS:
            merged[section] = {**data.get(section, {}), **own.get(section, {})}
        if "database" not in own:
            base = Path(data.get("database", "data/mojira.sqlite3"))
            merged["database"] = str(base.with_name(f"{base.stem}.{monitor_id}{base.suffix}"))
        configs.append(build_config(merged, path))
    if len({c.monitor_id for c in configs}) != len(configs):
        raise ValueError("监控 id 不可重复")
    if len({c.database for c in configs}) != len(configs):
        raise ValueError("每个监控项必须使用不同的 database，避免规则相互覆盖")
    return configs


def load_config(path: str | Path) -> Config:
    configs = load_configs(path)
    if len(configs) != 1:
        raise ValueError("包含多个监控项，请使用 load_configs 或 CLI --monitor 选择")
    return configs[0]


def select_configs(configs: list[Config], ids: list[str] | None) -> list[Config]:
    if ids:
        unknown = set(ids) - {c.monitor_id for c in configs}
        if unknown:
            raise ValueError("未知监控 id: " + ", ".join(sorted(unknown)))
        selected = [c for c in configs if c.monitor_id in ids]
    else:
        selected = [c for c in configs if c.enabled]
    if not selected:
        raise ValueError("没有启用的监控项；请设置 enabled=true 或使用 --monitor 指定")
    return selected
