"""Typed records that cross the collector boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PAPER_FIELDS = (
    "title",
    "published",
    "updated",
    "authors",
    "link",
    "pdf_url",
    "summary",
)

FUNCTIONAL_SOURCES = (
    "arxiv",
    "semantic_scholar",
    "github",
    "official_docs",
    "huggingface",
)
UNSUPPORTED_SOURCES = (
    "google_scholar",
    "papers_with_code",
)
ALL_SOURCES = FUNCTIONAL_SOURCES + UNSUPPORTED_SOURCES

EXIT_OK = 0
EXIT_SOURCE_FAILURE = 1
EXIT_USAGE = 2

SUCCESS_STATUSES = frozenset({"ok", "empty", "unsupported", "skipped"})

SUMMARY_CAP = 8000


class UsageError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Evidence:
    provider: str
    url: str
    version: str | None
    kind: str
    observation: str
    demonstration: str
    detail: str
    supplied_by_caller: bool = False
    production_state: str = "not_production"
    production_label_rejected: bool = False
    basis: str = "automatic"

    def public_dict(self) -> dict[str, Any]:
        verified = self.production_state == "production_verified"
        return {
            "provider": self.provider,
            "url": self.url,
            "version": self.version,
            "kind": self.kind,
            "observation": self.observation,
            "demonstration": self.demonstration,
            "detail": self.detail,
            "supplied_by_caller": self.supplied_by_caller,
            "basis": self.basis,
            "production_state": self.production_state,
            "production_verified": verified,
            "production_label_rejected": self.production_label_rejected,
        }


@dataclass
class Article:
    title: str
    published: str
    updated: str
    authors: tuple[str, ...]
    link: str
    pdf_url: str
    summary: str
    provider: str
    source_url: str
    doi: str | None
    arxiv_id: str | None
    arxiv_version: str | None
    pdf_version_verified: bool
    year: int | None = None
    summary_truncated: bool = False
    pdf_evidence: str = "absent"
    pdf_bytes_fetched: bool = False
    extra_provenance: dict[str, Any] = field(default_factory=dict)

    def paper_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "published": self.published,
            "updated": self.updated,
            "authors": list(self.authors),
            "link": self.link,
            "pdf_url": self.pdf_url,
            "summary": self.summary,
        }


@dataclass
class ReleaseNote:
    tag: str
    url: str
    name: str
    body_excerpt: str


@dataclass
class RepoSnapshot:
    owner: str
    name: str
    html_url: str
    description: str
    default_branch: str
    homepage: str
    readme_text: str
    readme_url: str
    releases: tuple[ReleaseNote, ...]
    code_file: str | None
    code_bytes: int
    code_sha256: str | None


@dataclass
class DocumentRecord:
    url: str
    provenance: str
    status: str
    title: str
    excerpt: str
    content_type: str
    observed_bytes: int
    related_repo: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "provenance": self.provenance,
            "status": self.status,
            "title": self.title,
            "excerpt": self.excerpt,
            "content_type": self.content_type,
            "observed_bytes": self.observed_bytes,
            "related_repo": self.related_repo,
            "body_stored": False,
        }


@dataclass
class SourceOutcome:
    provider: str
    status: str
    reason: str
    http_status: int | None
    credentials: str
    articles: list[Article] = field(default_factory=list)
    repos: list[RepoSnapshot] = field(default_factory=list)
    documents: list[DocumentRecord] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "reason": self.reason,
            "http_status": self.http_status,
            "credentials": self.credentials,
            "item_count": len(self.articles) + len(self.repos) + len(self.documents),
            "live_verified": False,
            "rejected": _public_rejections(self.rejected),
        }


def _public_rejections(items: list[dict[str, str]]) -> list[dict[str, str]]:
    public: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        public.append(
            {
                "repo": str(item.get("repo") or "")[:200],
                "reason": str(item.get("reason") or "")[:80],
            }
        )
    return public
