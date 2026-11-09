"""Hugging Face paper metadata only.

Official reads: GET /api/papers/search and GET /api/papers/{arxiv_id}.
No model, dataset, or weight download. No trust_remote_code. Metadata does not
authorize execution. This module is deferred from the principal branch and is
fixture-tested only.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from multisource.http_client import USER_AGENT, HttpClient
from multisource.identity import pdf_metadata_matches, query_identifier, valid_date
from multisource.models import SUMMARY_CAP, Article, SourceOutcome
from multisource.sources.http_status import failure_status, http_failure, json_payload

_SEARCH = "https://huggingface.co/api/papers/search"
_PAPER = "https://huggingface.co/api/papers/"
_PAPER_ID = re.compile(r"^\d{4}\.\d{4,5}$")
_REPO = re.compile(r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)/?$")
_REJECT_MAGIC = (b"\x80", b"PK", b"\x1f\x8b")


def _category_query(query: str) -> bool:
    upper = query.upper()
    return query.lower().startswith("cat:") or " AND " in upper or " OR " in upper


class HuggingFaceSource:
    provider = "huggingface"

    def collect(
        self,
        query: str,
        limit: int,
        token: str | None,
        client: HttpClient,
        timeout: float,
    ) -> SourceOutcome:
        credentials = "provided" if token else "not_provided"
        if _category_query(query):
            return SourceOutcome(
                self.provider,
                "skipped",
                "huggingface_does_not_interpret_arxiv_category_syntax",
                None,
                credentials,
            )
        ident = query_identifier(query)
        if ident and ident[0] == "arxiv":
            url = _PAPER + ident[1]
        else:
            url = _SEARCH + "?" + urlencode({"q": query, "limit": limit})
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = client.get_provider(self.provider, url, headers, timeout)
        except Exception as exc:  # noqa: BLE001
            status, reason = failure_status(exc)
            return SourceOutcome(self.provider, status, reason, None, credentials)
        if response.body[:3] in _REJECT_MAGIC or response.body.startswith(b"\x80"):
            return SourceOutcome(self.provider, "upstream_error", "rejected_payload", response.status, credentials)
        failed = http_failure(response, key_provided=bool(token))
        if failed == "not_found":
            return SourceOutcome(self.provider, "empty", "not_indexed", response.status, credentials)
        if failed:
            creds = "rejected" if failed == "rejected_credentials" else credentials
            return SourceOutcome(self.provider, failed, failed, response.status, creds)
        try:
            payload = json_payload(response)
        except ValueError:
            return SourceOutcome(self.provider, "upstream_error", "invalid_json", response.status, credentials)
        try:
            rows = _rows(payload)
        except ValueError:
            return SourceOutcome(self.provider, "upstream_error", "invalid_payload", response.status, credentials)
        articles = [item for item in (_article(row, url) for row in rows) if item is not None]
        if rows and not articles:
            return SourceOutcome(self.provider, "upstream_error", "missing_stable_id", response.status, credentials)
        status = "ok" if articles else "empty"
        return SourceOutcome(self.provider, status, "", response.status, credentials, articles=articles)


def _rows(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("papers", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if "id" in payload or "paper" in payload:
            return [payload]
    raise ValueError("invalid_payload")


def _article(row: object, source_url: str) -> Article | None:
    if not isinstance(row, dict):
        return None
    paper = row.get("paper") if isinstance(row.get("paper"), dict) else row
    if not isinstance(paper, dict):
        return None
    raw_id = paper.get("id")
    if not isinstance(raw_id, str) or not _PAPER_ID.fullmatch(raw_id):
        return None
    title = paper.get("title") or row.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    summary = paper.get("summary") if isinstance(paper.get("summary"), str) else ""
    truncated = False
    if len(summary) > SUMMARY_CAP:
        summary = summary[:SUMMARY_CAP]
        truncated = True
    published = paper.get("publishedAt") if isinstance(paper.get("publishedAt"), str) else ""
    published = published[:10]
    if not valid_date(published):
        published = ""
    authors: list[str] = []
    for author in paper.get("authors") or []:
        if isinstance(author, str) and author.strip():
            authors.append(author.strip()[:200])
        elif isinstance(author, dict):
            name = author.get("name") or author.get("fullname") or author.get("user")
            if isinstance(name, str) and name.strip():
                authors.append(name.strip()[:200])
        if len(authors) >= 50:
            break
    extra: dict[str, object] = {
        "huggingface_paper_id": raw_id,
        "authorizes_execution": False,
    }
    candidate = _code_candidate(paper)
    if candidate is not None:
        extra["code_candidate"] = candidate
    elif isinstance(paper.get("githubRepo"), str):
        extra["observed_repo_without_revision"] = paper.get("githubRepo")
    pdf_url = ""
    return Article(
        title=" ".join(title.split()),
        published=published,
        updated="",
        authors=tuple(authors),
        link=f"https://huggingface.co/papers/{raw_id}",
        pdf_url=pdf_url,
        summary=summary,
        provider="huggingface",
        source_url=source_url,
        doi=None,
        arxiv_id=raw_id,
        arxiv_version=None,
        pdf_version_verified=pdf_metadata_matches(raw_id, None, pdf_url),
        pdf_evidence="absent",
        pdf_bytes_fetched=False,
        extra_provenance=extra,
    )


def _code_candidate(paper: dict) -> dict[str, object] | None:
    raw = paper.get("githubRepo")
    revision = paper.get("githubRevision") or paper.get("revision") or paper.get("sha")
    if not isinstance(raw, str) or not isinstance(revision, str) or not revision.strip():
        return None
    match = _REPO.match(raw.strip())
    if not match:
        return None
    return {
        "repo": f"{match.group('owner')}/{match.group('name')}",
        "revision": revision.strip()[:80],
        "url": raw.strip(),
        "authorizes_execution": False,
    }
