"""Normalize provider results into paper JSON plus a separate audit manifest."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from multisource.briefing import render_briefing
from multisource.http_client import HttpClient, Resolver
from multisource.identity import canonical_link, match_repo_to_article, normalize_doi, repo_identity
from multisource.models import (
    SUCCESS_STATUSES,
    Article,
    DocumentRecord,
    Evidence,
    SourceOutcome,
)
from multisource.practical import Decision, classify
from multisource.sources.arxiv_source import ArxivSource
from multisource.sources.github_source import GitHubSource
from multisource.sources.official_docs import OfficialDocsSource
from multisource.sources.semantic_scholar import SemanticScholarSource
from multisource.sources.unsupported import GoogleScholarSource, PapersWithCodeSource

_RANK = {"arxiv": 0, "semantic_scholar": 1}


@dataclass
class SuppliedEvidence:
    arxiv_id: str | None
    arxiv_version: str | None
    doi: str | None
    evidence: Evidence


@dataclass
class RunConfig:
    query: str
    sources: tuple[str, ...]
    limit: int
    github_repos: tuple[str, ...] = ()
    docs_urls: tuple[str, ...] = ()
    supplied: tuple[SuppliedEvidence, ...] = ()
    timeout: float = 20
    github_token: str | None = None
    semantic_scholar_token: str | None = None


@dataclass
class Record:
    article: Article
    provenance: list[dict[str, object]]
    code_links: list[dict[str, object]] = field(default_factory=list)
    rejected_code: list[dict[str, str]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    decision: Decision | None = None
    related: list[str] = field(default_factory=list)


@dataclass
class Bundle:
    outcomes: list[SourceOutcome]
    records: list[Record]
    documents: list[DocumentRecord]
    unlinked_code: list[dict[str, str]]
    briefing: str
    papers: list[dict[str, object]]
    exit_code: int
    retrieved_at: str


def _provenance(article: Article, retrieved_at: str) -> dict[str, object]:
    return {
        "provider": article.provider,
        "source_url": article.source_url,
        "observed": True,
        "retrieved_at": retrieved_at,
        "year": article.year,
        "summary_truncated": article.summary_truncated,
        "live_verified": False,
    }


def _prefer(left: Article, right: Article) -> Article:
    primary, secondary = (left, right) if _RANK.get(left.provider, 9) <= _RANK.get(right.provider, 9) else (right, left)
    if primary.pdf_url:
        pdf_url = primary.pdf_url
        verified = primary.pdf_version_verified
        pdf_evidence = primary.pdf_evidence
    else:
        pdf_url = secondary.pdf_url
        verified = secondary.pdf_version_verified
        pdf_evidence = secondary.pdf_evidence
    return replace(
        primary,
        pdf_url=pdf_url,
        pdf_version_verified=verified,
        pdf_evidence=pdf_evidence,
        pdf_bytes_fetched=False,
        doi=normalize_doi(primary.doi) or normalize_doi(secondary.doi),
        arxiv_id=primary.arxiv_id or secondary.arxiv_id,
        arxiv_version=primary.arxiv_version or secondary.arxiv_version,
        summary=primary.summary or secondary.summary,
        published=primary.published or secondary.published,
        updated=primary.updated or secondary.updated,
    )


def _versions_conflict(left: Article, right: Article) -> bool:
    if left.arxiv_id and right.arxiv_id and left.arxiv_id != right.arxiv_id:
        return True
    if left.arxiv_version and right.arxiv_version and left.arxiv_version != right.arxiv_version:
        return True
    return False


def _can_alias(left: Article, right: Article) -> bool:
    """Merge stable aliases. Conflicting arXiv ids or versions stay separate."""
    if _versions_conflict(left, right):
        return False
    if left.arxiv_id and right.arxiv_id and left.arxiv_id == right.arxiv_id:
        return True
    left_doi = normalize_doi(left.doi)
    right_doi = normalize_doi(right.doi)
    if left_doi and right_doi and left_doi == right_doi:
        return True
    if not left.arxiv_id and not right.arxiv_id and not left.doi and not right.doi:
        return bool(left.link) and canonical_link(left.link) == canonical_link(right.link)
    return False


def _version_relation(group: list[Article]) -> str:
    present = {item.arxiv_version for item in group if item.arxiv_version}
    if not present:
        return "unspecified"
    if any(not item.arxiv_version for item in group):
        return "compatible_unspecified"
    return "exact"


def dedup_articles(
    articles: list[Article], retrieved_at: str
) -> list[tuple[Article, list[dict[str, object]]]]:
    parent = list(range(len(articles)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left in range(len(articles)):
        for right in range(left):
            if _can_alias(articles[left], articles[right]):
                union(left, right)
    buckets: dict[int, list[int]] = {}
    for index in range(len(articles)):
        buckets.setdefault(find(index), []).append(index)
    grouped: list[tuple[Article, list[dict[str, object]]]] = []
    for indexes in buckets.values():
        merged = articles[indexes[0]]
        provenance = [_provenance(articles[indexes[0]], retrieved_at)]
        members = [articles[indexes[0]]]
        for index in indexes[1:]:
            merged = _prefer(merged, articles[index])
            provenance.append(_provenance(articles[index], retrieved_at))
            members.append(articles[index])
        extra = dict(merged.extra_provenance)
        extra["version_relation"] = _version_relation(members)
        merged = replace(merged, extra_provenance=extra)
        grouped.append((merged, provenance))
    return grouped


def _repo_evidence(repo) -> list[Evidence]:
    items: list[Evidence] = []
    if repo.code_file and repo.code_sha256:
        items.append(
            Evidence(
                provider="github",
                url=repo.html_url,
                version=repo.default_branch or None,
                kind="code",
                observation="observed",
                demonstration="unverified",
                detail=f"Fetched {repo.code_file} ({repo.code_bytes} bytes); not executed.",
                production_state="inspected_unverified",
                basis="automatic",
            )
        )
    for release in repo.releases:
        items.append(
            Evidence(
                provider="github",
                url=release.url,
                version=release.tag,
                kind="release",
                observation="observed",
                demonstration="author_demonstration",
                detail=f"Release {release.tag} fetched; not executed.",
                production_state="author_claim",
                basis="automatic",
            )
        )
    if not items:
        items.append(
            Evidence(
                provider="github",
                url=repo.html_url,
                version=None,
                kind="repo_link",
                observation="observed",
                demonstration="unverified",
                detail="Identifier matched. A repository link is not production evidence.",
                production_state="not_production",
                basis="automatic",
            )
        )
    return items


def _attaches(article: Article, supplied: SuppliedEvidence) -> bool:
    doi_match = bool(supplied.doi and article.doi and supplied.doi == article.doi)
    arxiv_match = bool(supplied.arxiv_id and article.arxiv_id and supplied.arxiv_id == article.arxiv_id)
    if not doi_match and not arxiv_match:
        return False
    if supplied.arxiv_version and article.arxiv_version and supplied.arxiv_version != article.arxiv_version:
        return False
    if supplied.arxiv_version and not article.arxiv_version:
        return False
    return True


def _version_conflict(article: Article, found: set[tuple[str, str, str | None]]) -> bool:
    if match_repo_to_article(article, found):
        return False
    for kind, value, version in found:
        if kind == "arxiv" and article.arxiv_id == value and version and article.arxiv_version and version != article.arxiv_version:
            return True
    return False


def run_collection(
    config: RunConfig,
    client: HttpClient,
    resolver: Resolver,
    clock,
) -> Bundle:
    retrieved_at = clock()
    outcomes: list[SourceOutcome] = []
    articles: list[Article] = []
    repos = []
    for name in config.sources:
        if name == "official_docs":
            continue
        outcome = _collect_source(name, config, client, resolver)
        outcomes.append(outcome)
        articles.extend(outcome.articles)
        repos.extend(outcome.repos)
    grouped = dedup_articles(articles, retrieved_at)
    records = [Record(article=article, provenance=provenance) for article, provenance in grouped]
    _link_related(records)
    unlinked: list[dict[str, str]] = []
    for repo in repos:
        found = repo_identity(repo)
        linked = False
        for record in records:
            match = match_repo_to_article(record.article, found)
            if match is None:
                if _version_conflict(record.article, found):
                    record.rejected_code.append(
                        {"repo": f"{repo.owner}/{repo.name}", "reason": "version_conflict"}
                    )
                continue
            linked = True
            record.code_links.append(
                {
                    "repo": f"{repo.owner}/{repo.name}",
                    "html_url": repo.html_url,
                    "match": match,
                    "homepage": repo.homepage,
                    "code_file": repo.code_file,
                    "code_sha256": repo.code_sha256,
                    "releases": [
                        {"tag": item.tag, "url": item.url, "excerpt": item.body_excerpt}
                        for item in repo.releases
                    ],
                }
            )
            record.evidence.extend(_repo_evidence(repo))
        if not linked:
            repo_name = f"{repo.owner}/{repo.name}"
            conflict = any(
                item.get("repo") == repo_name and item.get("reason") == "version_conflict"
                for record in records
                for item in record.rejected_code
            )
            unlinked.append(
                {"repo": repo_name, "reason": "version_conflict" if conflict else "no_shared_identifier"}
            )
    for record in records:
        for supplied in config.supplied:
            if _attaches(record.article, supplied):
                record.evidence.append(supplied.evidence)
        decision, normalized = classify(record.evidence)
        record.evidence = normalized
        record.decision = decision
    documents: list[DocumentRecord] = []
    if "official_docs" in config.sources:
        pairs: list[tuple[str, str, str | None]] = [
            (url, "explicit", None) for url in config.docs_urls
        ]
        seen: set[str] = set()
        for record in records:
            for link in record.code_links:
                homepage = str(link.get("homepage") or "")
                repo_name = str(link.get("repo") or "")
                if homepage and homepage not in seen:
                    seen.add(homepage)
                    pairs.append((homepage, "github_homepage", repo_name))
        docs_outcome = OfficialDocsSource().collect(pairs[:8], client, resolver, config.timeout)
        outcomes.append(docs_outcome)
        documents = list(docs_outcome.documents)
    exit_code = 0 if all(item.status in SUCCESS_STATUSES for item in outcomes) else 1
    papers = [record.article.paper_dict() for record in records]
    briefing = render_briefing(_briefing_items(records))
    return Bundle(outcomes, records, documents, unlinked, briefing, papers, exit_code, retrieved_at)


def _collect_source(name: str, config: RunConfig, client: HttpClient, resolver: Resolver) -> SourceOutcome:
    if name == "arxiv":
        return ArxivSource().collect(config.query, config.limit, client, config.timeout)
    if name == "semantic_scholar":
        return SemanticScholarSource().collect(
            config.query,
            config.limit,
            config.semantic_scholar_token,
            client,
            resolver,
            config.timeout,
        )
    if name == "github":
        return GitHubSource().collect(
            config.query,
            config.github_repos,
            config.limit,
            config.github_token,
            client,
            config.timeout,
        )
    if name == "google_scholar":
        return GoogleScholarSource().collect()
    if name == "papers_with_code":
        return PapersWithCodeSource().collect()
    return SourceOutcome(name, "upstream_error", "unknown_source", None, "not_used")


def _link_related(records: list[Record]) -> None:
    groups: dict[str, list[Record]] = {}
    for record in records:
        if record.article.arxiv_id:
            groups.setdefault(record.article.arxiv_id, []).append(record)
    for group in groups.values():
        if len(group) < 2:
            continue
        for record in group:
            record.related = [
                other.article.link
                for other in group
                if other is not record and other.article.link != record.article.link
            ]


def _briefing_items(records: list[Record]) -> list[dict[str, object]]:
    items = []
    for record in records:
        decision = record.decision
        detail = ""
        for evidence in record.evidence:
            if evidence.kind == "practical_result" and evidence.demonstration == "independent_reproduction":
                detail = evidence.detail
                break
        items.append(
            {
                "title": record.article.title,
                "link": record.article.link,
                "provider": record.article.provider,
                "practical": decision,
                "production_state": decision.production_state if decision else "not_production",
                "production_verified": False,
                "independent_reproduction": bool(decision and decision.independent_reproduction),
                "evidence_detail": detail,
            }
        )
    return items


def summary_sha256(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def manifest_dict(bundle: Bundle, papers_sha256: str | None) -> dict[str, object]:
    articles = []
    for record in bundle.records:
        decision = record.decision.public_dict() if record.decision else {}
        article = record.article
        articles.append(
            {
                "title": article.title,
                "published": article.published,
                "updated": article.updated,
                "authors": list(article.authors),
                "link": article.link,
                "pdf_url": article.pdf_url,
                "pdf_version_verified": article.pdf_version_verified,
                "pdf_evidence": article.pdf_evidence,
                "pdf_bytes_fetched": False,
                "pdf_bytes_verified": False,
                "version_relation": article.extra_provenance.get("version_relation", ""),
                "summary_sha256": summary_sha256(article.summary),
                "summary_chars": len(article.summary),
                "summary_included": False,
                "identities": {
                    "doi": article.doi,
                    "arxiv_id": article.arxiv_id,
                    "arxiv_version": article.arxiv_version,
                    "canonical": canonical_link(article.link),
                },
                "provenance": record.provenance,
                "code_links": record.code_links,
                "rejected_code": record.rejected_code,
                "related_versions": record.related,
                "evidence": [item.public_dict() for item in record.evidence],
                "practical": decision,
            }
        )
    return {
        "schema_version": 1,
        "retrieved_at": bundle.retrieved_at,
        "live_verified": False,
        "production_verified": False,
        "production_activation": False,
        "tcc_eligibility_decided_here": False,
        "certifies_running": False,
        "independent_reproduction_is_not_production": True,
        "papers_sha256": papers_sha256,
        "sources": [item.public_dict() for item in bundle.outcomes],
        "articles": articles,
        "unlinked_code": bundle.unlinked_code,
        "documents": [item.public_dict() for item in bundle.documents],
        "pdf_bytes_stored": False,
    }
