from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp requires a timezone")
    return dt.astimezone(timezone.utc)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "li", "div"}:
            self.parts.append("\n")


def plain_text(value) -> str:
    """Handle ADF objects, JSON-encoded ADF, legacy text, and HTML."""
    if value is None:
        return ""
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return plain_text(json.loads(value))
            except (ValueError, RecursionError):
                pass
        if re.search(r"</?(?:p|div|ul|li|br|span)\b", value, re.I):
            parser = _HTMLText()
            parser.feed(value)
            return "".join(parser.parts).strip()
        return value
    if isinstance(value, list):
        return "".join(plain_text(v) for v in value)
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "text":
            return str(value.get("text", ""))
        if kind == "hardBreak":
            return "\n"
        text = plain_text(value.get("content", []))
        if kind in {"paragraph", "heading", "codeBlock", "listItem", "blockquote"}:
            text += "\n"
        return text
    return ""


@dataclass
class Issue:
    key: str
    summary: str
    updated_date: str
    resolution: str
    description: str = ""
    environment: str = ""
    status: str | None = None
    created_date: str | None = None
    affected_versions: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    source: str = "official"
    raw: dict = field(default_factory=dict)
    full: bool = True

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", self.key):
            raise ValueError("invalid issue key")
        if not isinstance(self.summary, str) or not isinstance(self.resolution, str):
            raise ValueError("invalid issue fields")
        self.updated_date = iso(parse_time(self.updated_date))

    @property
    def duplicate(self):
        return self.resolution.strip().casefold() == "duplicate"

    @property
    def duplicate_of(self):
        return next((link["key"] for link in self.links if link.get("type", "").casefold() == "duplicates"), None)

    @property
    def text(self):
        return f"{self.summary}\n{self.description}\n{self.environment}".strip()

    @property
    def url(self):
        project = self.key.split("-")[0]
        return f"https://bugs.mojang.com/browse/{project}/issues/{self.key}"

    def snapshot(self):
        data = asdict(self)
        data["url"] = self.url
        data["duplicate_of"] = self.duplicate_of
        return data


def from_official(data: dict) -> Issue:
    f = data["fields"]
    # Missing resolution is a schema error, not proof of an unresolved issue.
    resolution = f["resolution"]
    links = []
    for link in f.get("issuelinks") or []:
        for direction, name in (("outwardIssue", "outward"), ("inwardIssue", "inward")):
            if direction in link:
                links.append({"type": link["type"][name], "key": link[direction]["key"]})
    return Issue(
        key=data["key"], summary=f["summary"], updated_date=f["updated"],
        resolution=resolution["name"] if resolution else "Unresolved",
        description=plain_text(f.get("description")), environment=plain_text(f.get("environment")),
        status=(f.get("status") or {}).get("name"), created_date=f.get("created"),
        affected_versions=[v["name"] for v in f.get("versions") or []],
        fix_versions=[v["name"] for v in f.get("fixVersions") or []],
        labels=f.get("labels") or [], links=links,
        attachments=[{k: a.get(k) for k in ("id", "filename", "size", "mimeType")} for a in f.get("attachment") or []],
        raw=data, full="description" in f,
    )


def from_mirror(data: dict, *, full: bool) -> Issue:
    return Issue(
        key=data["key"], summary=data["summary"], updated_date=data["updated_date"],
        resolution=data["resolution"], description=plain_text(data.get("description")),
        environment=plain_text(data.get("environment")), status=data.get("status"),
        created_date=data.get("created_date"), affected_versions=data.get("affected_versions") or [],
        fix_versions=data.get("fix_versions") or [], labels=data.get("labels") or [],
        source="mirror", raw=data, full=full,
    )
