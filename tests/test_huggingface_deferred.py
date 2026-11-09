import json
import unittest

from multisource.http_client import BlockedUrl, HttpClient, HttpRequest, HttpResponse, assert_provider_url
from multisource.pipeline import RunConfig, run_collection
from multisource.sources.huggingface import HuggingFaceSource


class _Fake:
    def __init__(self, handler):
        self.handler = handler

    def send(self, request: HttpRequest) -> HttpResponse:
        return self.handler(request)


def _response(status, body, url):
    payload = body if isinstance(body, bytes) else body.encode()
    return HttpResponse(status, url, {}, payload)


def _resolver(_host):
    return ["8.8.8.8"]


class HuggingFaceDeferredTests(unittest.TestCase):
    def test_category_query_does_not_fetch(self):
        def explode(request):
            raise AssertionError(request.url)

        outcome = HuggingFaceSource().collect(
            "cat:cs.LG OR cat:cs.AI",
            1,
            None,
            HttpClient(_Fake(explode)),
            1,
        )
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.articles, [])

    def test_stable_id_and_revision_do_not_authorize_execution(self):
        payload = [
            {
                "title": "Placeholder title only",
                "paper": {
                    "id": "2101.00001",
                    "title": "Placeholder Attention Note",
                    "summary": "Short placeholder summary.",
                    "publishedAt": "2021-01-02T00:00:00.000Z",
                    "authors": [{"name": "Ada Placeholder"}],
                    "githubRepo": "https://github.com/octo/demo",
                    "githubRevision": "abc123",
                },
            },
            {"paper": {"title": "No stable id"}},
        ]

        def handler(request):
            self.assertNotIn("HFSECRET", request.url)
            self.assertEqual(request.headers.get("Authorization"), "Bearer HFSECRET")
            return _response(200, json.dumps(payload), request.url)

        bundle = run_collection(
            RunConfig(
                query="placeholder attention",
                sources=("huggingface",),
                limit=1,
                timeout=1,
                huggingface_token="HFSECRET",
            ),
            HttpClient(_Fake(handler)),
            _resolver,
            lambda: "2026-01-01T00:00:00Z",
        )
        self.assertEqual(bundle.exit_code, 0)
        self.assertEqual(len(bundle.papers), 1)
        self.assertEqual(bundle.records[0].article.arxiv_id, "2101.00001")
        self.assertEqual(bundle.records[0].code_links[0]["revision"], "abc123")
        self.assertFalse(bundle.records[0].code_links[0]["authorizes_execution"])
        self.assertFalse(bundle.records[0].article.extra_provenance["authorizes_execution"])
        self.assertNotIn("HFSECRET", json.dumps(bundle.outcomes[0].public_dict()))

    def test_repo_without_revision_is_not_identity(self):
        payload = [
            {
                "paper": {
                    "id": "2101.00001",
                    "title": "Placeholder",
                    "summary": "x",
                    "publishedAt": "2021-01-02",
                    "githubRepo": "https://github.com/octo/demo",
                }
            }
        ]
        outcome = HuggingFaceSource().collect(
            "2101.00001",
            1,
            None,
            HttpClient(_Fake(lambda request: _response(200, json.dumps(payload), request.url))),
            1,
        )
        self.assertEqual(outcome.articles[0].link, "https://huggingface.co/papers/2101.00001")
        self.assertNotIn("code_candidate", outcome.articles[0].extra_provenance)
        self.assertIn("observed_repo_without_revision", outcome.articles[0].extra_provenance)

    def test_model_and_pickle_payloads_are_rejected(self):
        with self.assertRaises(BlockedUrl):
            assert_provider_url("huggingface", "https://huggingface.co/api/models/org/name")
        outcome = HuggingFaceSource().collect(
            "placeholder",
            1,
            None,
            HttpClient(_Fake(lambda request: _response(200, b"\x80\x04pickle", request.url))),
            1,
        )
        self.assertEqual(outcome.status, "upstream_error")
        self.assertEqual(outcome.reason, "rejected_payload")
        self.assertEqual(outcome.articles, [])


if __name__ == "__main__":
    unittest.main()
