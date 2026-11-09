"""Fetch explicit document URLs or one discovered repository homepage. No crawl."""

from __future__ import annotations

from html.parser import HTMLParser

from multisource.http_client import USER_AGENT, HttpClient, Resolver, assert_public_https, BlockedUrl
from multisource.models import DocumentRecord, SourceOutcome
from multisource.sources.http_status import failure_status, http_failure


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self.in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def _excerpt(html: bytes) -> tuple[str, str]:
    parser = _VisibleText()
    parser.feed(html.decode("utf-8", errors="replace"))
    title = " ".join("".join(parser.title_parts).split())[:180]
    text = " ".join("".join(parser.parts).split())[:400]
    return title, text


def _media_type(headers: dict[str, str]) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


class OfficialDocsSource:
    provider = "official_docs"

    def validate_explicit(self, urls: tuple[str, ...], resolver: Resolver) -> None:
        for url in urls:
            assert_public_https(url, resolver)

    def collect(
        self,
        urls: list[tuple[str, str, str | None]],
        client: HttpClient,
        resolver: Resolver,
        timeout: float,
    ) -> SourceOutcome:
        if not urls:
            return SourceOutcome(self.provider, "skipped", "no_explicit_or_discovered_url", None, "not_used")
        documents: list[DocumentRecord] = []
        for url, provenance, repo in urls[:8]:
            try:
                assert_public_https(url, resolver)
            except BlockedUrl as exc:
                documents.append(
                    DocumentRecord(url, provenance, "skipped", "", "", "", 0, repo)
                )
                documents[-1].status = "skipped"
                _ = exc
                continue
            record, failure = self._fetch(url, provenance, repo, client, resolver, timeout)
            if failure is not None:
                return failure
            if record is not None:
                documents.append(record)
        status = "ok" if documents else "empty"
        return SourceOutcome(self.provider, status, "", None, "not_used", documents=documents)

    def _fetch(self, url, provenance, repo, client, resolver, timeout):
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"}
        try:
            response = client.get_public(url, headers, timeout, resolver)
        except Exception as exc:  # noqa: BLE001
            status, reason = failure_status(exc)
            return None, SourceOutcome(self.provider, status, reason, None, "not_used")
        failed = http_failure(response, key_provided=False)
        if failed:
            return None, SourceOutcome(self.provider, failed, failed, response.status, "not_used")
        media = _media_type(dict(response.headers))
        if "pdf" in media or response.body.startswith(b"%PDF"):
            return (
                DocumentRecord(response.url or url, provenance, "pdf_not_inlined", "", "", media, len(response.body), repo),
                None,
            )
        title, excerpt = _excerpt(response.body)
        return (
            DocumentRecord(response.url or url, provenance, "ok", title, excerpt, media, len(response.body), repo),
            None,
        )
