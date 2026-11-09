"""DOI, arXiv id/version, and repository identity. Name similarity is not identity."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from multisource.models import Article, RepoSnapshot

ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?",
    re.IGNORECASE,
)
ARXIV_LABEL_RE = re.compile(
    r"\barxiv\s*[:.]?\s*(\d{4}\.\d{4,5})(v\d+)?",
    re.IGNORECASE,
)
ARXIV_BARE_RE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
ARXIV_PDF_RE = re.compile(
    r"^https?://(?:www\.|export\.)?arxiv\.org/pdf/(?P<id>\d{4}\.\d{4,5})(?P<ver>v\d+)?(?:\.pdf)?/?$",
    re.IGNORECASE,
)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[A-Za-z0-9._;()/:+\-]+)", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_doi(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE)
    text = text.strip().strip(" .)")
    match = DOI_RE.fullmatch(text)
    if not match:
        found = DOI_RE.search(text)
        if not found:
            return None
        text = found.group(1).strip(" .)")
    return text.lower()


def parse_arxiv_from_url(url: str) -> tuple[str | None, str | None]:
    match = ARXIV_URL_RE.search(url or "")
    if not match:
        return None, None
    return match.group(1), match.group(2)


def canonical_pdf_identity(url: str) -> tuple[str | None, str | None]:
    """Exact arXiv PDF id and version. v1 does not match v10."""
    match = ARXIV_PDF_RE.match((url or "").strip())
    if not match:
        return None, None
    return match.group("id"), match.group("ver")


def pdf_metadata_matches(article_id: str | None, article_version: str | None, url: str) -> bool:
    pdf_id, pdf_version = canonical_pdf_identity(url)
    if not pdf_id or not article_id or pdf_id.lower() != article_id.lower():
        return False
    if not article_version or not pdf_version:
        return False
    return pdf_version.lower() == article_version.lower()


def identifiers_in_text(text: str) -> set[tuple[str, str, str | None]]:
    found: set[tuple[str, str, str | None]] = set()
    for regex in (ARXIV_URL_RE, ARXIV_LABEL_RE):
        for match in regex.finditer(text or ""):
            found.add(("arxiv", match.group(1), match.group(2)))
    for match in DOI_RE.finditer(text or ""):
        doi = normalize_doi(match.group(1))
        if doi:
            found.add(("doi", doi, None))
    return found


def query_identifier(query: str) -> tuple[str, str, str | None] | None:
    """Use GitHub search only when the query is a single explicit id, not a harvest."""
    text = (query or "").strip()
    if not text:
        return None
    upper = text.upper()
    if " AND " in upper or " OR " in upper or text.lower().startswith("cat:"):
        return None
    bare = ARXIV_BARE_RE.fullmatch(text)
    if bare:
        return ("arxiv", bare.group(1), bare.group(2))
    labeled = list(ARXIV_LABEL_RE.finditer(text))
    urls = list(ARXIV_URL_RE.finditer(text))
    dois = []
    for match in DOI_RE.finditer(text):
        doi = normalize_doi(match.group(1))
        if doi:
            dois.append(doi)
    total = len(labeled) + len(urls) + len(dois)
    if total != 1:
        return None
    if labeled:
        match = labeled[0]
        return ("arxiv", match.group(1), match.group(2))
    if urls:
        match = urls[0]
        return ("arxiv", match.group(1), match.group(2))
    return ("doi", dois[0], None)


def canonical_link(url: str) -> str:
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    path = (parts.path or "").rstrip("/")
    scheme = "https" if parts.scheme in {"http", "https"} else parts.scheme
    if not host:
        return url.strip()
    return f"{scheme}://{host}{path}"


def valid_date(value: str) -> bool:
    return bool(DATE_RE.fullmatch(value or ""))


def repo_identity(repo: RepoSnapshot) -> set[tuple[str, str, str | None]]:
    text = "\n".join([repo.description or "", repo.readme_text or ""])
    return identifiers_in_text(text)


def match_repo_to_article(
    article: Article,
    found: set[tuple[str, str, str | None]],
) -> str | None:
    """Return exact, id_only, or None. A conflicting version is not a match."""
    saw_version_conflict = False
    id_only = False
    for kind, value, version in found:
        if kind == "doi" and article.doi and value == article.doi:
            return "exact"
        if kind != "arxiv" or not article.arxiv_id or value != article.arxiv_id:
            continue
        if version and article.arxiv_version and version != article.arxiv_version:
            saw_version_conflict = True
            continue
        if version and article.arxiv_version and version == article.arxiv_version:
            return "exact"
        if version and not article.arxiv_version:
            saw_version_conflict = True
            continue
        if not version:
            id_only = True
    if id_only and not saw_version_conflict:
        return "id_only"
    return None
