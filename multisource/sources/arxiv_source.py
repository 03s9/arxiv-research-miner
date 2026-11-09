"""arXiv API adapter. Reuses the existing Atom parser without changing it."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from arxiv_miner import parse_arxiv_response
from multisource.http_client import USER_AGENT, HttpClient
from multisource.identity import parse_arxiv_from_url, pdf_metadata_matches, valid_date
from multisource.models import SUMMARY_CAP, Article, SourceOutcome
from multisource.sources.http_status import failure_status, http_failure

_API = "https://export.arxiv.org/api/query"


def observed_pdf_hrefs(xml_bytes: bytes) -> list[str | None]:
    """PDF hrefs actually present in the feed. None means the legacy parser would synthesize one."""
    root = ET.fromstring(xml_bytes)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    observed: list[str | None] = []
    for entry in root.findall("atom:entry", namespace):
        href = None
        for link_el in entry.findall("atom:link", namespace):
            if link_el.attrib.get("title") == "pdf" and link_el.attrib.get("href"):
                href = link_el.attrib.get("href")
                break
        observed.append(href)
    return observed


def _article_from_parsed(paper: object, source_url: str, observed_pdf: str | None) -> Article:
    link = str(getattr(paper, "link"))
    pdf_url = str(getattr(paper, "pdf_url") or "")
    arxiv_id, version = parse_arxiv_from_url(link)
    summary = str(getattr(paper, "summary") or "")
    truncated = False
    if len(summary) > SUMMARY_CAP:
        summary = summary[:SUMMARY_CAP]
        truncated = True
    published = str(getattr(paper, "published") or "")
    updated = str(getattr(paper, "updated") or "")
    authors = tuple(str(name) for name in getattr(paper, "authors") if str(name).strip())
    return Article(
        title=str(getattr(paper, "title") or "").strip(),
        published=published if valid_date(published) else "",
        updated=updated if valid_date(updated) else "",
        authors=authors,
        link=link,
        pdf_url=pdf_url,
        summary=summary,
        provider="arxiv",
        source_url=source_url,
        doi=None,
        arxiv_id=arxiv_id,
        arxiv_version=version,
        pdf_version_verified=bool(
            observed_pdf and pdf_metadata_matches(arxiv_id, version, observed_pdf)
        ),
        summary_truncated=truncated,
        pdf_evidence="observed_metadata" if observed_pdf else "inferred_url",
        pdf_bytes_fetched=False,
    )


class ArxivSource:
    provider = "arxiv"

    def collect(self, query: str, limit: int, client: HttpClient, timeout: float) -> SourceOutcome:
        url = _API + "?" + urlencode(
            {
                "search_query": query,
                "start": 0,
                "max_results": limit,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml"}
        try:
            response = client.get_provider(self.provider, url, headers, timeout)
        except Exception as exc:  # noqa: BLE001 — mapped, headers are not copied
            status, reason = failure_status(exc)
            return SourceOutcome(self.provider, status, reason, None, "not_used")
        failed = http_failure(response, key_provided=False)
        if failed:
            return SourceOutcome(self.provider, failed, failed, response.status, "not_used")
        try:
            papers = parse_arxiv_response(response.body)
            observed = observed_pdf_hrefs(response.body)
        except Exception:
            return SourceOutcome(self.provider, "upstream_error", "invalid_feed", response.status, "not_used")
        if len(observed) != len(papers):
            return SourceOutcome(self.provider, "upstream_error", "pdf_evidence_mismatch", response.status, "not_used")
        articles = [
            item
            for item in (
                _article_from_parsed(paper, url, href) for paper, href in zip(papers, observed)
            )
            if item.title
        ]
        status = "ok" if articles else "empty"
        return SourceOutcome(self.provider, status, "", response.status, "not_used", articles=articles)
