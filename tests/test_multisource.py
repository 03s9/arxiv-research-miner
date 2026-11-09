import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from arxiv_miner import Paper
from multisource.cli import main, resolve_query
from multisource.http_client import (
    BlockedUrl,
    HttpClient,
    HttpRequest,
    HttpResponse,
    OfflineError,
    UrllibTransport,
    assert_provider_url,
    assert_public_https,
)
from multisource.identity import match_repo_to_article, pdf_metadata_matches, repo_identity
from multisource.models import Article, Evidence, ReleaseNote, RepoSnapshot, SourceOutcome
from multisource.pipeline import RunConfig, dedup_articles, run_collection
from multisource.practical import classify
from multisource.sources.arxiv_source import ArxivSource
from multisource.sources.github_source import GitHubSource
from multisource.sources.semantic_scholar import SemanticScholarSource
from multisource.sources.unsupported import GoogleScholarSource, PapersWithCodeSource

SENTINEL = "SENTINEL_ABSTRACT_SHOULD_NOT_ENTER_BRIEFING"
ATOM = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00001v1</id>
    <published>2021-01-02T00:00:00Z</published>
    <updated>2021-01-03T00:00:00Z</updated>
    <title>Placeholder Attention Note</title>
    <summary>{SENTINEL}</summary>
    <author><name>Ada Placeholder</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/2101.00001v1.pdf" type="application/pdf"/>
  </entry>
</feed>
""".encode()


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.calls.append(request)
        return self.handler(request)


def _response(status, body, url="https://export.arxiv.org/api/query", headers=None):
    payload = body if isinstance(body, bytes) else body.encode()
    return HttpResponse(status, url, headers or {}, payload)


def _article(**overrides) -> Article:
    data = {
        "title": "Placeholder Attention Note",
        "published": "2021-01-02",
        "updated": "2021-01-03",
        "authors": ("Ada Placeholder",),
        "link": "https://arxiv.org/abs/2101.00001v1",
        "pdf_url": "https://arxiv.org/pdf/2101.00001v1.pdf",
        "summary": SENTINEL,
        "provider": "arxiv",
        "source_url": "https://export.arxiv.org/api/query",
        "doi": None,
        "arxiv_id": "2101.00001",
        "arxiv_version": "v1",
        "pdf_version_verified": True,
    }
    data.update(overrides)
    return Article(**data)


def _public_resolver(_host: str) -> list[str]:
    return ["8.8.8.8"]


class CompatibilityTests(unittest.TestCase):
    def test_paper_fields_stay_compatible(self):
        paper = Paper("T", "2021-01-02", "2021-01-03", ["Ada Placeholder"], "http://arxiv.org/abs/1", "", "s")
        self.assertEqual(
            list(paper.to_dict().keys()),
            ["title", "published", "updated", "authors", "link", "pdf_url", "summary"],
        )

    def test_topic_one_query(self):
        query = resolve_query("1", None, Path("topics.json"))
        self.assertEqual(query, "cat:cs.LG OR cat:cs.AI")


class HttpPolicyTests(unittest.TestCase):
    def test_provider_rejects_http_userinfo_and_off_host(self):
        with self.assertRaises(BlockedUrl):
            assert_provider_url("arxiv", "http://export.arxiv.org/api/query")
        with self.assertRaises(BlockedUrl):
            assert_provider_url("github", "https://user:secret@api.github.com/repos/a/b")
        with self.assertRaises(BlockedUrl):
            assert_provider_url("semantic_scholar", "https://example.org/graph/v1/paper/search")

    def test_public_url_rejects_metadata_and_private_dns(self):
        with self.assertRaises(BlockedUrl):
            assert_public_https("https://127.0.0.1/docs", _public_resolver)
        with self.assertRaises(BlockedUrl):
            assert_public_https("https://169.254.169.254/latest", _public_resolver)
        with self.assertRaises(BlockedUrl):
            assert_public_https("https://docs.example", lambda _host: ["10.0.0.8"])

    def test_redirect_off_allowlist_is_not_followed(self):
        transport = FakeTransport(
            lambda request: _response(
                302,
                b"",
                request.url,
                {"location": "https://evil.example/api/query"},
            )
        )
        client = HttpClient(transport)
        with self.assertRaises(BlockedUrl):
            client.get_provider(
                "arxiv",
                "https://export.arxiv.org/api/query?search_query=x",
                {},
                5,
            )
        self.assertEqual(len(transport.calls), 1)

    def test_retry_is_bounded_and_rate_limit_is_not_retried(self):
        calls = {"n": 0, "arxiv": 0}

        def handler(request):
            calls["n"] += 1
            if "semantic" in request.url:
                return _response(429, b"Too Many Requests", request.url)
            calls["arxiv"] += 1
            if calls["arxiv"] == 1:
                return _response(503, b"", request.url)
            return _response(200, ATOM, request.url)

        transport = FakeTransport(handler)
        client = HttpClient(transport)
        limited = client.get_provider(
            "semantic_scholar",
            "https://api.semanticscholar.org/graph/v1/paper/search?query=x",
            {},
            5,
        )
        self.assertEqual(limited.status, 429)
        self.assertEqual(calls["n"], 1)
        ok = client.get_provider("arxiv", "https://export.arxiv.org/api/query?q=1", {}, 5)
        self.assertEqual(ok.status, 200)
        self.assertEqual(calls["n"], 3)

    def test_offline_transport_does_not_pass(self):
        with mock.patch.dict(os.environ, {"MULTISOURCE_OFFLINE": "1"}):
            with self.assertRaises(OfflineError):
                UrllibTransport().send(
                    HttpRequest("GET", "https://export.arxiv.org/api/query", {}, 1)
                )


class PracticalTests(unittest.TestCase):
    def test_theory_link_and_author_claim_are_not_production(self):
        theory, _ = classify(
            [Evidence("arxiv", "https://example.org/a", None, "abstract_claim", "observed", "unverified", "claim")]
        )
        self.assertEqual(theory.recommendation, "excluded_theory_only")
        self.assertFalse(theory.production_verified)
        link, _ = classify(
            [Evidence("github", "https://github.com/octo/demo", None, "repo_link", "observed", "unverified", "link")]
        )
        self.assertEqual(link.recommendation, "excluded_insufficient_evidence")
        self.assertFalse(link.production_verified)
        author, _ = classify(
            [
                Evidence(
                    "github",
                    "https://github.com/octo/demo/releases/v1",
                    "v1",
                    "release",
                    "observed",
                    "author_demonstration",
                    "implemented in production",
                    production_state="production_verified",
                )
            ]
        )
        self.assertEqual(author.production_state, "author_claim")
        self.assertFalse(author.production_verified)
        self.assertFalse(author.certifies_running)
        self.assertTrue(author.production_label_rejected)
        self.assertFalse(author.independent_reproduction)

    def test_independent_reproduction_is_not_production_verification(self):
        decision, _ = classify(
            [
                Evidence(
                    "github",
                    "https://github.com/octo/demo",
                    "main",
                    "code",
                    "observed",
                    "unverified",
                    "Fetched main.py; not executed.",
                ),
                Evidence(
                    "caller",
                    "https://example.org/notes/result",
                    "v1",
                    "practical_result",
                    "observed",
                    "independent_reproduction",
                    "Separate reproduction record.",
                    supplied_by_caller=True,
                    basis="explicit_record",
                ),
            ]
        )
        self.assertTrue(decision.independent_reproduction)
        self.assertFalse(decision.production_verified)
        self.assertFalse(decision.public_dict()["production_verified"])
        self.assertFalse(decision.public_dict()["certifies_running"])
        self.assertEqual(decision.recommendation, "accepted_on_supplied_evidence")

    def test_production_attestation_does_not_verify_production_or_running(self):
        decision, normalized = classify(
            [
                Evidence(
                    "caller",
                    "https://example.org/system",
                    "v1",
                    "production_usage",
                    "observed",
                    "independent_reproduction",
                    "Caller says this is used in production.",
                    supplied_by_caller=True,
                    production_state="production_verified",
                    basis="explicit_record",
                )
            ]
        )
        self.assertEqual(normalized[0].production_state, "attested_unverified")
        self.assertFalse(decision.production_verified)
        self.assertFalse(decision.certifies_running)
        self.assertTrue(decision.production_label_rejected)
        self.assertNotEqual(decision.production_state, "production_verified")


class CollectionTests(unittest.TestCase):
    def test_arxiv_export_manifest_and_briefing_contract(self):
        transport = FakeTransport(lambda request: _response(200, ATOM, request.url))
        bundle = run_collection(
            RunConfig(query="cat:cs.LG OR cat:cs.AI", sources=("arxiv",), limit=1, timeout=5),
            HttpClient(transport),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        self.assertEqual(bundle.exit_code, 0)
        self.assertEqual(bundle.outcomes[0].status, "ok")
        self.assertEqual(len(bundle.papers), 1)
        self.assertEqual(list(bundle.papers[0].keys()), list(Paper("", "", "", [], "", "", "").to_dict().keys()))
        self.assertIn(SENTINEL, bundle.papers[0]["summary"])
        self.assertNotIn(SENTINEL, bundle.briefing)
        self.assertIn("**MELHORIA / ALTERAÇÃO / FEATURE**", bundle.briefing)
        self.assertIn("production_verified=false", bundle.briefing)
        self.assertFalse(bundle.records[0].decision.production_verified)
        requested = transport.calls[0].url
        self.assertTrue(requested.startswith("https://export.arxiv.org/api/query?"))
        self.assertIn("max_results=1", requested)
        self.assertIn("sortBy=submittedDate", requested)

    def test_semantic_scholar_empty_rate_limit_and_redaction(self):
        secret = "super-secret-key-value"
        seen = {}

        def handler(request):
            seen["key"] = request.headers.get("x-api-key")
            if "empty" in request.url:
                return _response(200, json.dumps({"total": 0, "data": []}), request.url)
            return _response(429, b"Too Many Requests", request.url)

        transport = FakeTransport(handler)
        client = HttpClient(transport)
        limited = run_collection(
            RunConfig(
                query="placeholder",
                sources=("semantic_scholar",),
                limit=1,
                semantic_scholar_token=secret,
                timeout=5,
            ),
            client,
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        self.assertEqual(limited.outcomes[0].status, "rate_limited")
        self.assertNotEqual(limited.outcomes[0].status, "empty")
        self.assertEqual(seen["key"], secret)
        self.assertNotIn(secret, json.dumps(limited.outcomes[0].public_dict()))
        empty = run_collection(
            RunConfig(query="empty", sources=("semantic_scholar",), limit=1, timeout=5),
            HttpClient(FakeTransport(handler)),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        self.assertEqual(empty.outcomes[0].status, "empty")
        self.assertEqual(empty.outcomes[0].http_status, 200)

    def test_s2_does_not_invent_a_day_from_year(self):
        payload = {
            "total": 1,
            "data": [
                {
                    "paperId": "abc123",
                    "title": "Placeholder Attention Note",
                    "abstract": SENTINEL,
                    "year": 2021,
                    "publicationDate": None,
                    "authors": [{"name": "Ada Placeholder"}],
                    "url": "https://www.semanticscholar.org/paper/abc123",
                    "externalIds": {"DOI": "10.1000/placeholder"},
                    "openAccessPdf": None,
                }
            ],
        }
        transport = FakeTransport(lambda request: _response(200, json.dumps(payload), request.url))
        bundle = run_collection(
            RunConfig(query="placeholder", sources=("semantic_scholar",), limit=1, timeout=5),
            HttpClient(transport),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        paper = bundle.papers[0]
        self.assertEqual(paper["published"], "")
        self.assertEqual(paper["updated"], "")
        self.assertEqual(bundle.records[0].article.doi, "10.1000/placeholder")
        self.assertNotIn("x-api-key", transport.calls[0].headers)

    def test_unsupported_sources_do_not_fetch(self):
        transport = FakeTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url)))
        bundle = run_collection(
            RunConfig(query="", sources=("google_scholar", "papers_with_code"), limit=1),
            HttpClient(transport),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        self.assertEqual(transport.calls, [])
        self.assertEqual([item.status for item in bundle.outcomes], ["unsupported", "unsupported"])
        self.assertEqual(bundle.exit_code, 0)
        self.assertIn("no official public API", GoogleScholarSource().collect().reason)
        self.assertIn("discontinued", PapersWithCodeSource().collect().reason)

    def test_github_category_query_is_skipped_and_homonym_is_not_evidence(self):
        skipped = GitHubSource().collect("cat:cs.LG OR cat:cs.AI", (), 1, None, HttpClient(FakeTransport(lambda r: r)), 5)
        self.assertEqual(skipped.status, "skipped")
        repo = {
            "html_url": "https://github.com/octo/demo",
            "description": "unrelated",
            "homepage": "",
            "default_branch": "main",
        }
        readme = {"encoding": "base64", "content": "dW5yZWxhdGVkIHByb2plY3Q=", "html_url": "https://github.com/octo/demo"}

        def handler(request):
            if request.url.endswith("/readme"):
                return _response(200, json.dumps(readme), request.url)
            if "/releases" in request.url:
                return _response(200, "[]", request.url)
            if request.url.endswith("/contents/"):
                return _response(200, "[]", request.url)
            return _response(200, json.dumps(repo), request.url)

        found = GitHubSource().collect("", ("octo/demo",), 1, None, HttpClient(FakeTransport(handler)), 5)
        self.assertEqual(found.status, "ok")
        identity = repo_identity(found.repos[0])
        self.assertIsNone(match_repo_to_article(_article(), identity))

    def test_matching_readme_links_and_does_not_mark_production(self):
        readme = "Study of arXiv:2101.00001v1\nproduction ready claim"
        import base64

        encoded = base64.b64encode(readme.encode()).decode()
        repo = {
            "html_url": "https://github.com/octo/demo",
            "description": "",
            "homepage": "https://docs.example/guide",
            "default_branch": "main",
        }
        release = [
            {
                "tag_name": "v0.1.0",
                "html_url": "https://github.com/octo/demo/releases/tag/v0.1.0",
                "name": "v0.1.0",
                "body": "implemented in production",
            }
        ]

        def handler(request):
            url = request.url
            if "export.arxiv.org" in url:
                return _response(200, ATOM, url)
            if url.endswith("/readme"):
                return _response(200, json.dumps({"encoding": "base64", "content": encoded, "html_url": url}), url)
            if "/releases" in url:
                return _response(200, json.dumps(release), url)
            if url.endswith("/contents/"):
                listing = [{"type": "file", "name": "main.py", "size": 12}]
                return _response(200, json.dumps(listing), url)
            if url.endswith("/main.py"):
                content = base64.b64encode(b"print('placeholder')\n").decode()
                return _response(200, json.dumps({"encoding": "base64", "content": content}), url)
            if "docs.example" in url:
                html = "<html><title>Placeholder Docs</title><script>SECRET_SCRIPT</script><p>Visible note.</p></html>"
                return _response(200, html, url, {"content-type": "text/html"})
            return _response(200, json.dumps(repo), url)

        bundle = run_collection(
            RunConfig(
                query="2101.00001v1",
                sources=("arxiv", "github", "official_docs"),
                limit=1,
                github_repos=("octo/demo",),
                timeout=5,
            ),
            HttpClient(FakeTransport(handler)),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        self.assertEqual(bundle.records[0].code_links[0]["repo"], "octo/demo")
        self.assertEqual(bundle.records[0].code_links[0]["match"], "exact")
        self.assertFalse(bundle.records[0].decision.production_verified)
        self.assertFalse(bundle.records[0].decision.independent_reproduction)
        self.assertNotEqual(bundle.records[0].decision.production_state, "production_verified")
        self.assertNotIn(SENTINEL, json.dumps(bundle.records[0].code_links))
        self.assertTrue(bundle.documents)
        self.assertNotIn("SECRET_SCRIPT", bundle.documents[0].excerpt)
        self.assertNotIn("%PDF", json.dumps([item.public_dict() for item in bundle.documents]))

    def test_version_conflict_is_not_identity(self):
        snapshot = RepoSnapshot(
            "octo",
            "demo",
            "https://github.com/octo/demo",
            "",
            "main",
            "",
            "see arXiv:2101.00001v2",
            "https://github.com/octo/demo",
            (),
            None,
            0,
            None,
        )
        self.assertIsNone(match_repo_to_article(_article(), repo_identity(snapshot)))

    def test_doi_dedup_and_distinct_versions(self):
        first = _article(doi="10.1000/placeholder", provider="arxiv")
        second = _article(
            provider="semantic_scholar",
            doi="10.1000/placeholder",
            link="https://www.semanticscholar.org/paper/abc123",
            pdf_url="",
            source_url="https://api.semanticscholar.org/graph/v1/paper/search",
        )
        other_version = _article(arxiv_version="v2", link="https://arxiv.org/abs/2101.00001v2")
        merged = dedup_articles([first, second, other_version], "2026-01-01T00:00:00Z")
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(merged[0][1]), 2)

    def test_rate_limit_does_not_write_papers(self):
        transport = FakeTransport(lambda request: _response(429, b"rate limit", request.url))
        bundle = run_collection(
            RunConfig(query="placeholder", sources=("semantic_scholar",), limit=1, timeout=5),
            HttpClient(transport),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        self.assertEqual(bundle.exit_code, 1)
        from tempfile import TemporaryDirectory
        from multisource.cli import emit

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers.json"
            manifest = root / "manifest.json"
            digest = emit(bundle, papers, manifest, None)
            self.assertIsNone(digest)
            self.assertFalse(papers.exists())
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIsNone(saved["papers_sha256"])
            self.assertFalse(saved["production_verified"])
            self.assertFalse(saved["certifies_running"])
            self.assertFalse(saved["tcc_eligibility_decided_here"])
            self.assertEqual(saved["sources"][0]["status"], "rate_limited")

    def test_successful_hash_and_existing_output_refusal(self):
        transport = FakeTransport(lambda request: _response(200, ATOM, request.url))
        bundle = run_collection(
            RunConfig(query="placeholder", sources=("arxiv",), limit=1, timeout=5),
            HttpClient(transport),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        from tempfile import TemporaryDirectory
        from multisource.cli import emit
        from multisource.models import UsageError

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers.json"
            manifest = root / "manifest.json"
            briefing = root / "briefing.md"
            digest = emit(bundle, papers, manifest, briefing)
            self.assertEqual(digest, __import__("hashlib").sha256(papers.read_bytes()).hexdigest())
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(saved["papers_sha256"], digest)
            self.assertNotIn(SENTINEL, manifest.read_text(encoding="utf-8"))
            self.assertNotIn(SENTINEL, briefing.read_text(encoding="utf-8"))
            self.assertIn(SENTINEL, papers.read_text(encoding="utf-8"))
            with self.assertRaises(UsageError):
                emit(bundle, papers, manifest, briefing)


class CliTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_list_sources_and_secret_redaction(self):
        secret = "super-secret-key-value"
        with mock.patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": secret, "GITHUB_TOKEN": secret}):
            code, output, _err = self._run(["--list-sources"])
        self.assertEqual(code, 0)
        self.assertIn("google_scholar: unsupported", output)
        self.assertIn("papers_with_code: unsupported", output)
        self.assertNotIn(secret, output)

    def test_scholar_status_without_network(self):
        with mock.patch.dict(os.environ, {"MULTISOURCE_OFFLINE": "1"}):
            code, output, _err = self._run(["--sources", "google_scholar"])
        self.assertEqual(code, 0)
        self.assertIn("status=unsupported", output)
        self.assertIn("live_verified=false", output)

    def test_blocked_docs_url_exits_before_success(self):
        with mock.patch.dict(os.environ, {"MULTISOURCE_OFFLINE": "1"}):
            code, _out, err = self._run(
                ["--sources", "official_docs", "--docs-url", "https://127.0.0.1/secret"]
            )
        self.assertEqual(code, 2)
        self.assertIn("error:", err)

    def test_missing_topic_is_usage_error(self):
        code, _out, err = self._run(["--sources", "arxiv", "--topic", "missing"])
        self.assertEqual(code, 2)
        self.assertIn("topic_not_found", err)


class ReviewRegressionTests(unittest.TestCase):
    def test_nat64_and_compatible_literals_do_not_touch_the_network(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("network used")

        urls = [
            "https://[64:ff9b::169.254.169.254]/latest/meta-data",
            "https://[64:ff9b::7f00:1]/",
            "https://[::169.254.169.254]/",
        ]
        with mock.patch("socket.getaddrinfo", explode), mock.patch("socket.create_connection", explode):
            for url in urls:
                with self.assertRaises(BlockedUrl):
                    assert_public_https(url, explode)

    def test_rebinding_does_not_connect_when_any_address_is_unsafe(self):
        def connector(ip, host, request):
            raise AssertionError(f"connected {ip}")

        transport = UrllibTransport(resolver=lambda _host: ["8.8.8.8", "10.1.2.3"], connector=connector)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MULTISOURCE_OFFLINE", None)
            with self.assertRaises(BlockedUrl):
                transport.send(HttpRequest("GET", "https://export.arxiv.org/api/query?q=1", {}, 1))

    def test_pinned_connect_uses_the_resolved_ip(self):
        seen = {}

        def connector(ip, host, request):
            seen["ip"] = ip
            seen["host"] = host
            return HttpResponse(200, request.url, {}, b"ok")

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MULTISOURCE_OFFLINE", None)
            UrllibTransport(resolver=lambda _host: ["8.8.8.8"], connector=connector).send(
                HttpRequest("GET", "https://export.arxiv.org/api/query?q=1", {}, 1)
            )
        self.assertEqual(seen, {"ip": "8.8.8.8", "host": "export.arxiv.org"})

    def test_redirect_hop_refuses_private_resolution_without_a_second_connect(self):
        connects = []

        def resolver(_host):
            return ["8.8.8.8"] if not connects else ["fd00::1"]

        def connector(ip, host, request):
            connects.append(ip)
            return HttpResponse(
                302,
                request.url,
                {"location": "https://export.arxiv.org/api/query?q=2"},
                b"",
            )

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MULTISOURCE_OFFLINE", None)
            client = HttpClient(UrllibTransport(resolver=resolver, connector=connector))
            with self.assertRaises(BlockedUrl):
                client.get_provider("arxiv", "https://export.arxiv.org/api/query?q=1", {}, 1)
        self.assertEqual(connects, ["8.8.8.8"])

    def test_cross_source_arxiv_id_merges_and_versions_do_not(self):
        arxiv = _article(doi=None, provider="arxiv", arxiv_version="v1")
        semantic = _article(
            provider="semantic_scholar",
            doi="10.48550/arXiv.2101.00001",
            arxiv_id="2101.00001",
            arxiv_version="v1",
            link="https://www.semanticscholar.org/paper/abc123",
            pdf_url="",
            source_url="https://api.semanticscholar.org/graph/v1/paper/search",
        )
        merged = dedup_articles([arxiv, semantic], "2026-01-01T00:00:00Z")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][0].arxiv_id, "2101.00001")
        self.assertEqual(merged[0][0].arxiv_version, "v1")
        self.assertEqual(merged[0][0].doi, "10.48550/arxiv.2101.00001")
        unspecified = _article(
            provider="semantic_scholar",
            doi=None,
            arxiv_id="2101.00001",
            arxiv_version=None,
            link="https://www.semanticscholar.org/paper/abc123",
            pdf_url="",
        )
        self.assertEqual(len(dedup_articles([arxiv, unspecified], "2026-01-01T00:00:00Z")), 1)
        later = _article(
            doi="10.48550/arxiv.2101.00001",
            arxiv_version="v2",
            link="https://arxiv.org/abs/2101.00001v2",
        )
        self.assertEqual(len(dedup_articles([arxiv, later], "2026-01-01T00:00:00Z")), 2)

    def test_pdf_version_match_is_exact_and_synthesized_is_not_verified(self):
        self.assertTrue(pdf_metadata_matches("2101.00001", "v1", "https://arxiv.org/pdf/2101.00001v1.pdf"))
        self.assertFalse(pdf_metadata_matches("2101.00001", "v1", "https://arxiv.org/pdf/2101.00001v10.pdf"))
        self.assertFalse(pdf_metadata_matches("2101.00001", "v1", "https://arxiv.org/pdf/2101.00002v1.pdf"))
        self.assertFalse(pdf_metadata_matches("2101.00001", "v1", "https://arxiv.org/pdf/2101.00001.pdf"))
        feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2101.00001v1</id>
    <published>2021-01-02T00:00:00Z</published>
    <updated>2021-01-02T00:00:00Z</updated>
    <title>Placeholder</title>
    <summary>SENTINEL_ABSTRACT_SHOULD_NOT_ENTER_BRIEFING</summary>
    <author><name>Ada Placeholder</name></author>
  </entry>
</feed>
"""
        bundle = run_collection(
            RunConfig(query="id", sources=("arxiv",), limit=1, timeout=1),
            HttpClient(FakeTransport(lambda request: _response(200, feed, request.url))),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        article = bundle.records[0].article
        self.assertEqual(article.pdf_evidence, "inferred_url")
        self.assertFalse(article.pdf_version_verified)
        self.assertFalse(article.pdf_bytes_fetched)
        payload = {
            "total": 1,
            "data": [
                {
                    "paperId": "abc123",
                    "title": "Placeholder",
                    "abstract": "x",
                    "publicationDate": "2021-01-02",
                    "authors": [{"name": "Ada Placeholder"}],
                    "url": "https://www.semanticscholar.org/paper/abc123",
                    "externalIds": {"ArXiv": "2101.00001v1", "DOI": "10.48550/arXiv.2101.00001"},
                    "openAccessPdf": {"url": "https://arxiv.org/pdf/2101.00001v10.pdf"},
                }
            ],
        }
        semantic = SemanticScholarSource().collect(
            "placeholder",
            1,
            None,
            HttpClient(FakeTransport(lambda request: _response(200, json.dumps(payload), request.url))),
            _public_resolver,
            1,
        )
        self.assertFalse(semantic.articles[0].pdf_version_verified)
        self.assertEqual(semantic.articles[0].pdf_evidence, "observed_metadata")
        self.assertFalse(semantic.articles[0].pdf_bytes_fetched)

    def test_equal_export_paths_are_rejected_before_write(self):
        from tempfile import TemporaryDirectory

        bundle = run_collection(
            RunConfig(query="", sources=("google_scholar",), limit=1),
            HttpClient(FakeTransport(lambda request: (_ for _ in ()).throw(AssertionError("network")))),
            _public_resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        from multisource.cli import emit
        from multisource.models import UsageError

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = root / "papers.json"
            alias = root / "nested" / ".." / "papers.json"
            with self.assertRaises(UsageError):
                emit(bundle, papers, alias, None)
            self.assertFalse(papers.exists())
            link = root / "manifest.json"
            link.symlink_to("papers.json")
            with self.assertRaises(UsageError):
                emit(bundle, papers, link, None)
            self.assertFalse(papers.exists())

    def test_homonym_rejection_is_in_the_public_manifest_view(self):
        outcome = SourceOutcome(
            "github",
            "ok",
            "",
            200,
            "not_provided",
            rejected=[{"repo": "octo/demo", "reason": "homonym_or_missing_identifier", "readme": "secret-body"}],
        )
        public = outcome.public_dict()
        self.assertEqual(public["rejected"], [{"repo": "octo/demo", "reason": "homonym_or_missing_identifier"}])
        self.assertNotIn("secret-body", json.dumps(public))
        self.assertNotIn("huggingface", json.dumps(public))


class WorkflowTests(unittest.TestCase):
    def test_pull_request_workflow_is_offline_and_least_privilege(self):
        text = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertNotIn("pull_request_target", text)
        self.assertIn("contents: read", text)
        self.assertIn('python-version: "3.12"', text)
        self.assertIn(
            "MULTISOURCE_OFFLINE=1 PYTHONPATH=. python3 -m unittest discover -s tests",
            text,
        )
        self.assertIn("multisource.guardian --check --policy security/trusted-policy.json --root .", text)
        self.assertLess(
            text.index("multisource.guardian --check"),
            text.index("unittest discover"),
        )
        self.assertNotIn("guardian --run", text)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", text)
        self.assertIn("actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0", text)
        self.assertNotIn("secrets.", text)


if __name__ == "__main__":
    unittest.main()
