"""Semantic Scholar Academic Graph paper search. The key is optional."""

from __future__ import annotations

from urllib.parse import urlencode

from multisource.http_client import USER_AGENT, BlockedUrl, HttpClient, Resolver, assert_public_https
from multisource.identity import normalize_doi, parse_arxiv_from_url, pdf_metadata_matches, valid_date
from multisource.models import SUMMARY_CAP, Article, SourceOutcome
from multisource.sources.http_status import failure_status, http_failure, json_payload

_FIELDS = "paperId,title,abstract,authors,url,year,publicationDate,externalIds,openAccessPdf"
_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"


def _pdf_url(raw: object, resolver: Resolver) -> str:
    if not isinstance(raw, dict):
        return ""
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        return ""
    url = url.strip()
    try:
        assert_public_https(url, resolver)
    except BlockedUrl:
        return ""
    return url


class SemanticScholarSource:
    provider = "semantic_scholar"

    def collect(
        self,
        query: str,
        limit: int,
        token: str | None,
        client: HttpClient,
        resolver: Resolver,
        timeout: float,
    ) -> SourceOutcome:
        credentials = "provided" if token else "not_provided"
        url = _SEARCH + "?" + urlencode(
            {"query": query, "limit": limit, "offset": 0, "fields": _FIELDS}
        )
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["x-api-key"] = token
        try:
            response = client.get_provider(self.provider, url, headers, timeout)
        except Exception as exc:  # noqa: BLE001
            status, reason = failure_status(exc)
            return SourceOutcome(self.provider, status, reason, None, credentials)
        failed = http_failure(response, key_provided=bool(token))
        if failed:
            creds = "rejected" if failed == "rejected_credentials" else credentials
            return SourceOutcome(self.provider, failed, failed, response.status, creds)
        try:
            payload = json_payload(response)
        except ValueError:
            return SourceOutcome(self.provider, "upstream_error", "invalid_json", response.status, credentials)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return SourceOutcome(self.provider, "upstream_error", "invalid_payload", response.status, credentials)
        articles: list[Article] = []
        for row in payload["data"]:
            article = _article(row, url, resolver)
            if article is not None:
                articles.append(article)
        status = "ok" if articles else "empty"
        reason = "" if articles or payload.get("total") in (0, None) else ""
        if not articles and payload.get("total") == 0:
            status = "empty"
        return SourceOutcome(self.provider, status, reason, response.status, credentials, articles=articles)


def _article(row: object, source_url: str, resolver: Resolver) -> Article | None:
    if not isinstance(row, dict):
        return None
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    summary = row.get("abstract") if isinstance(row.get("abstract"), str) else ""
    truncated = False
    if len(summary) > SUMMARY_CAP:
        summary = summary[:SUMMARY_CAP]
        truncated = True
    authors: list[str] = []
    for author in row.get("authors") or []:
        if isinstance(author, dict) and isinstance(author.get("name"), str):
            name = " ".join(author["name"].split())
            if name:
                authors.append(name[:200])
        if len(authors) >= 50:
            break
    external = row.get("externalIds") if isinstance(row.get("externalIds"), dict) else {}
    arxiv_raw = external.get("ArXiv") or external.get("arXiv")
    arxiv_id, version = (None, None)
    if isinstance(arxiv_raw, str):
        if arxiv_raw.startswith("http"):
            arxiv_id, version = parse_arxiv_from_url(arxiv_raw)
        else:
            arxiv_id, version = parse_arxiv_from_url("https://arxiv.org/abs/" + arxiv_raw.strip())
    doi = normalize_doi(external.get("DOI") if isinstance(external.get("DOI"), str) else None)
    published = row.get("publicationDate") if isinstance(row.get("publicationDate"), str) else ""
    if not valid_date(published):
        published = ""
    year = row.get("year") if isinstance(row.get("year"), int) else None
    pdf_url = _pdf_url(row.get("openAccessPdf"), resolver)
    pdf_verified = bool(pdf_url) and pdf_metadata_matches(arxiv_id, version, pdf_url)
    link = ""
    raw_url = row.get("url")
    if isinstance(raw_url, str) and raw_url.startswith("https://www.semanticscholar.org/"):
        link = raw_url.strip()
    elif arxiv_id:
        link = f"https://arxiv.org/abs/{arxiv_id}{version or ''}"
    else:
        paper_id = row.get("paperId")
        if isinstance(paper_id, str) and paper_id.isalnum():
            link = f"https://www.semanticscholar.org/paper/{paper_id}"
    if not link:
        return None
    return Article(
        title=" ".join(title.split()),
        published=published,
        updated="",
        authors=tuple(authors),
        link=link,
        pdf_url=pdf_url,
        summary=summary,
        provider="semantic_scholar",
        source_url=source_url,
        doi=doi,
        arxiv_id=arxiv_id,
        arxiv_version=version,
        pdf_version_verified=pdf_verified,
        year=year,
        summary_truncated=truncated,
        pdf_evidence="observed_metadata" if pdf_url else "absent",
        pdf_bytes_fetched=False,
    )
