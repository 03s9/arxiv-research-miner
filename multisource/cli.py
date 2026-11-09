"""CLI for one bounded multisource collection. Credentials come from the environment only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from arxiv_miner import DEFAULT_CONFIG_PATH, load_topics
from multisource.http_client import (
    BlockedUrl,
    HttpClient,
    UrllibTransport,
    assert_evidence_url,
    assert_public_https,
    default_resolver,
)
from multisource.models import ALL_SOURCES, EXIT_USAGE, Evidence, UsageError
from multisource.pipeline import RunConfig, SuppliedEvidence, manifest_dict, run_collection
from multisource.sources.github_source import parse_repo_spec

_KINDS = {
    "code",
    "release",
    "system_or_demo",
    "practical_result",
    "abstract_claim",
    "repo_link",
    "production_usage",
}
_OBSERVATIONS = {"observed", "inferred"}
_DEMONSTRATIONS = {"unverified", "author_demonstration", "independent_reproduction"}
_SOURCE_HELP = (
    ("arxiv", "functional public API; no credential; fixture-tested; live_verified=false"),
    ("semantic_scholar", "functional public paper search; optional SEMANTIC_SCHOLAR_API_KEY; live_verified=false"),
    ("github", "functional for an explicit repo or a single arXiv id/DOI; optional GITHUB_TOKEN; live_verified=false"),
    ("official_docs", "functional for explicit https URLs and one linked homepage; no crawl; live_verified=false"),
    (
        "huggingface",
        "deferred metadata-only GET /api/papers/search or /api/papers/{id}; optional HF_TOKEN; "
        "no model download; live_verified=false",
    ),
    ("google_scholar", "unsupported; no official public API; no request"),
    ("papers_with_code", "unsupported; official API discontinued; no request"),
)


def resolve_query(topic: str | None, query: str | None, config: Path) -> str:
    if query:
        text = query.strip()
        if not text or len(text) > 500:
            raise UsageError("invalid_query")
        return text
    if not topic:
        raise UsageError("query_or_topic_required")
    matched = next((item for item in load_topics(config) if str(item.get("id")) == str(topic)), None)
    if not matched or not isinstance(matched.get("query"), str) or not matched["query"].strip():
        raise UsageError("topic_not_found")
    return matched["query"].strip()


def parse_sources(raw: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise UsageError("sources_required")
    unknown = [name for name in names if name not in ALL_SOURCES]
    if unknown:
        raise UsageError("unknown_source")
    if len(set(names)) != len(names):
        raise UsageError("duplicate_source")
    return names


def load_supplied(path: Path) -> tuple[SuppliedEvidence, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError("invalid_evidence_file") from exc
    rows = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 20:
        raise UsageError("invalid_evidence_file")
    supplied: list[SuppliedEvidence] = []
    for row in rows:
        if not isinstance(row, dict):
            raise UsageError("invalid_evidence_file")
        evidence_rows = row.get("evidence")
        if not isinstance(evidence_rows, list) or not evidence_rows or len(evidence_rows) > 10:
            raise UsageError("invalid_evidence_file")
        arxiv_id = row.get("arxiv_id")
        version = row.get("version")
        doi = row.get("doi")
        if arxiv_id is not None and not isinstance(arxiv_id, str):
            raise UsageError("invalid_evidence_file")
        if version is not None and not isinstance(version, str):
            raise UsageError("invalid_evidence_file")
        if doi is not None and not isinstance(doi, str):
            raise UsageError("invalid_evidence_file")
        if not arxiv_id and not doi:
            raise UsageError("invalid_evidence_file")
        for item in evidence_rows:
            supplied.append(
                SuppliedEvidence(
                    arxiv_id=arxiv_id,
                    arxiv_version=version,
                    doi=doi.lower() if isinstance(doi, str) else None,
                    evidence=_one_evidence(item),
                )
            )
    return tuple(supplied)


def _one_evidence(item: object) -> Evidence:
    if not isinstance(item, dict):
        raise UsageError("invalid_evidence_file")
    kind = item.get("kind")
    observation = item.get("observation")
    demonstration = item.get("demonstration")
    detail = item.get("detail")
    url = item.get("url")
    provider = item.get("provider")
    version = item.get("version")
    if (
        kind not in _KINDS
        or observation not in _OBSERVATIONS
        or demonstration not in _DEMONSTRATIONS
        or not isinstance(detail, str)
        or not detail.strip()
        or len(detail) > 300
        or not isinstance(url, str)
        or not isinstance(provider, str)
        or not provider.strip()
        or len(provider) > 40
    ):
        raise UsageError("invalid_evidence_file")
    try:
        assert_evidence_url(url)
    except BlockedUrl as exc:
        raise UsageError(exc.reason) from exc
    state = item.get("production_state", "not_production")
    if not isinstance(state, str):
        raise UsageError("invalid_evidence_file")
    return Evidence(
        provider=provider.strip(),
        url=url,
        version=version if isinstance(version, str) else None,
        kind=kind,
        observation=observation,
        demonstration=demonstration,
        detail=detail.strip(),
        supplied_by_caller=True,
        production_state=state,
        basis="explicit_record",
    )


def paper_bytes(papers: list[dict[str, object]]) -> bytes:
    return (json.dumps(papers, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _absent(path: Path | None) -> None:
    if path is not None and path.exists():
        raise UsageError("output_exists")


def assert_distinct_outputs(*paths: Path | None) -> None:
    """Reject equal paths, including symlink and .. aliases, before any write."""
    chosen = [path for path in paths if path is not None]
    resolved: list[Path] = []
    for path in chosen:
        try:
            resolved.append(path.resolve(strict=False))
        except OSError as exc:
            raise UsageError("output_path_unresolved") from exc
    if len(set(resolved)) != len(resolved):
        raise UsageError("output_paths_alias")


def emit(bundle, papers_path: Path | None, manifest_path: Path | None, briefing_path: Path | None) -> str | None:
    assert_distinct_outputs(papers_path, manifest_path, briefing_path)
    for path in (papers_path, manifest_path, briefing_path):
        _absent(path)
    digest = None
    raw_papers = b""
    write_papers = papers_path is not None and bundle.exit_code == 0
    if write_papers:
        raw_papers = paper_bytes(bundle.papers)
        digest = hashlib.sha256(raw_papers).hexdigest()
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(manifest_dict(bundle, digest), indent=2, ensure_ascii=False) + "\n"
        manifest_path.write_text(body, encoding="utf-8")
    if write_papers and papers_path is not None:
        papers_path.parent.mkdir(parents=True, exist_ok=True)
        papers_path.write_bytes(raw_papers)
    if briefing_path is not None and bundle.exit_code == 0:
        briefing_path.parent.mkdir(parents=True, exist_ok=True)
        briefing_path.write_text(bundle.briefing, encoding="utf-8")
    return digest


def _print_sources() -> None:
    for name, text in _SOURCE_HELP:
        print(f"{name}: {text}")


def _print_bundle(bundle, digest: str | None) -> None:
    for outcome in bundle.outcomes:
        http = "" if outcome.http_status is None else str(outcome.http_status)
        print(
            f"source={outcome.provider} status={outcome.status} http={http} "
            f"credentials={outcome.credentials} items={outcome.public_dict()['item_count']} "
            "live_verified=false"
        )
    print(f"papers={len(bundle.papers)}")
    print("production_verified=false")
    print("independent_reproduction_is_not_production=true")
    print("certifies_running=false")
    print(f"exit={bundle.exit_code}")
    if digest:
        print(f"papers_sha256={digest}")
    elif bundle.exit_code != 0:
        print("papers_json=not_written")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect papers from explicit public sources.")
    parser.add_argument("--query", "-q")
    parser.add_argument("--topic")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--sources", help="Comma-separated source names")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--github-repo", action="append", default=[])
    parser.add_argument("--docs-url", action="append", default=[])
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--export-json", type=Path)
    parser.add_argument("--export-manifest", type=Path)
    parser.add_argument("--export-briefing", type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--list-sources", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_sources:
        _print_sources()
        return 0
    try:
        if not args.sources:
            raise UsageError("sources_required")
        if args.limit < 1 or args.limit > 10:
            raise UsageError("invalid_limit")
        if args.timeout <= 0 or args.timeout > 60:
            raise UsageError("invalid_timeout")
        sources = parse_sources(args.sources)
        for spec in args.github_repo:
            if parse_repo_spec(spec) is None:
                raise UsageError("invalid_github_repo")
        query = ""
        needs_query = any(name in {"arxiv", "semantic_scholar", "huggingface"} for name in sources) or (
            "github" in sources and not args.github_repo
        )
        if needs_query or args.query or args.topic:
            query = resolve_query(args.topic, args.query, args.config)
        docs = tuple(args.docs_url)
        if docs and "official_docs" not in sources:
            raise UsageError("docs_source_not_selected")
        for url in docs:
            assert_public_https(url, default_resolver)
        supplied = load_supplied(args.evidence_file) if args.evidence_file else ()
        assert_distinct_outputs(args.export_json, args.export_manifest, args.export_briefing)
        for path in (args.export_json, args.export_manifest, args.export_briefing):
            _absent(path)
    except UsageError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return EXIT_USAGE
    except BlockedUrl as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return EXIT_USAGE
    config = RunConfig(
        query=query,
        sources=sources,
        limit=args.limit,
        github_repos=tuple(args.github_repo),
        docs_urls=docs,
        supplied=supplied,
        timeout=args.timeout,
        github_token=os.environ.get("GITHUB_TOKEN") or None,
        semantic_scholar_token=os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None,
        huggingface_token=os.environ.get("HF_TOKEN") or None,
    )
    bundle = run_collection(config, HttpClient(UrllibTransport()), default_resolver, _clock)
    digest = None
    try:
        digest = emit(bundle, args.export_json, args.export_manifest, args.export_briefing)
    except UsageError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return EXIT_USAGE
    _print_bundle(bundle, digest)
    return bundle.exit_code


def _clock() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    sys.exit(main())
