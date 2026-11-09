"""Allowlisted provider HTTP and public-document checks. No open crawl."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

USER_AGENT = "arxiv-research-miner/0.2 (+https://github.com/03s9/arxiv-research-miner)"
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 2
_REDIRECTS = {301, 302, 303, 307, 308}
_RETRY = {502, 503, 504}

PROVIDER_HOSTS = {
    "arxiv": frozenset({"export.arxiv.org"}),
    "semantic_scholar": frozenset({"api.semanticscholar.org"}),
    "github": frozenset({"api.github.com"}),
}
PROVIDER_PREFIXES = {
    "arxiv": ("/api/query",),
    "semantic_scholar": ("/graph/v1/paper/search",),
    "github": ("/search/repositories", "/repos/"),
}


class BlockedUrl(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OfflineError(Exception):
    pass


class TransportError(Exception):
    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    timeout: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes


Resolver = Callable[[str], list[str]]


def _hostname_block_reason(host: str) -> str | None:
    lowered = host.lower().rstrip(".")
    if lowered in {"localhost", "metadata.google.internal", "metadata.internal"}:
        return "blocked_host"
    if lowered.endswith((".local", ".internal", ".localhost")):
        return "blocked_host"
    return None


def _non_public_ip(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not address.is_global


def _split_https(url: str):
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise BlockedUrl("https_required")
    if parts.username or parts.password:
        raise BlockedUrl("userinfo_rejected")
    host = parts.hostname
    if not host:
        raise BlockedUrl("missing_host")
    blocked = _hostname_block_reason(host)
    if blocked:
        raise BlockedUrl(blocked)
    if parts.port not in (None, 443):
        raise BlockedUrl("port_rejected")
    if ".." in (parts.path or "").split("/"):
        raise BlockedUrl("path_rejected")
    return parts, host


def assert_provider_url(provider: str, url: str) -> None:
    if provider not in PROVIDER_HOSTS:
        raise BlockedUrl("unknown_provider")
    parts, host = _split_https(url)
    if host.lower() not in PROVIDER_HOSTS[provider]:
        raise BlockedUrl("host_not_allowlisted")
    path = parts.path or ""
    prefixes = PROVIDER_PREFIXES[provider]
    if not any(path.startswith(prefix) for prefix in prefixes):
        raise BlockedUrl("path_not_allowlisted")


def assert_evidence_url(url: str) -> None:
    """Syntactic check only. Evidence URLs are stored and never fetched."""
    parts, host = _split_https(url)
    if not parts.path:
        raise BlockedUrl("path_rejected")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    if _non_public_ip(host):
        raise BlockedUrl("non_public_ip")


def assert_public_https(url: str, resolver: Resolver) -> None:
    _parts, host = _split_https(url)
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if _non_public_ip(host):
            raise BlockedUrl("non_public_ip")
        return
    addresses = resolver(host)
    if not addresses:
        raise BlockedUrl("dns_failed")
    for address in addresses:
        if _non_public_ip(address):
            raise BlockedUrl("non_public_ip")


def default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedUrl("dns_failed") from exc
    found = [item[4][0] for item in infos]
    if not found:
        raise BlockedUrl("dns_failed")
    return found


def _lower_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(key).lower(): str(value) for key, value in headers.items()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


class UrllibTransport:
    def send(self, request: HttpRequest) -> HttpResponse:
        if os.environ.get("MULTISOURCE_OFFLINE") == "1":
            raise OfflineError("network disabled")
        outgoing = urllib.request.Request(
            request.url,
            headers=dict(request.headers),
            method=request.method,
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(outgoing, timeout=request.timeout) as response:
                body = response.read(MAX_BYTES + 1)
                return HttpResponse(
                    status=int(getattr(response, "status", 200)),
                    url=response.geturl(),
                    headers=_lower_headers(response.headers),
                    body=body,
                )
        except urllib.error.HTTPError as err:
            raw = err.read(MAX_BYTES + 1) if err.fp is not None else b""
            return HttpResponse(
                status=int(err.code),
                url=request.url,
                headers=_lower_headers(err.headers),
                body=raw,
            )
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TransportError("timeout", "timeout") from None
            raise TransportError("network", "network") from None
        except (TimeoutError, socket.timeout):
            raise TransportError("timeout", "timeout") from None


def rate_limited(response: HttpResponse) -> bool:
    if response.status == 429:
        return True
    remaining = response.headers.get("x-ratelimit-remaining")
    if response.status == 403 and remaining == "0":
        return True
    sample = response.body[:400].lower()
    if response.status == 403 and (
        b"rate limit" in sample or b"too many requests" in sample
    ):
        return True
    return False


def _retry_delay(response: HttpResponse) -> float | None:
    if response.status not in _RETRY:
        return None
    raw = response.headers.get("retry-after")
    if raw is None:
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds < 0 or seconds > 1:
        return None
    return seconds


class HttpClient:
    def __init__(self, transport: object, sleeper: Callable[[float], None] | None = None) -> None:
        self.transport = transport
        self.sleeper = sleeper or (lambda _seconds: None)

    def get_provider(
        self,
        provider: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        return self._follow(
            url,
            headers,
            timeout,
            lambda current: assert_provider_url(provider, current),
        )

    def get_public(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        resolver: Resolver,
    ) -> HttpResponse:
        return self._follow(
            url,
            headers,
            timeout,
            lambda current: assert_public_https(current, resolver),
        )

    def _follow(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        check: Callable[[str], None],
    ) -> HttpResponse:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            response = self._exchange(current, headers, timeout, check)
            if response.status not in _REDIRECTS:
                return response
            if hop >= MAX_REDIRECTS:
                raise TransportError("upstream", "too_many_redirects")
            location = response.headers.get("location")
            if not location:
                raise TransportError("upstream", "redirect_missing_location")
            current = urljoin(current, location)
        raise TransportError("upstream", "too_many_redirects")

    def _exchange(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        check: Callable[[str], None],
    ) -> HttpResponse:
        check(url)
        request = HttpRequest("GET", url, dict(headers), timeout)
        response = self._send(request)
        delay = _retry_delay(response)
        if delay is not None:
            self.sleeper(delay)
            check(url)
            response = self._send(request)
        if len(response.body) > MAX_BYTES:
            raise TransportError("too_large", "response_too_large")
        return response

    def _send(self, request: HttpRequest) -> HttpResponse:
        send = getattr(self.transport, "send")
        try:
            response = send(request)
        except (OfflineError, TransportError, BlockedUrl):
            raise
        except Exception:
            raise TransportError("network", "network") from None
        if not isinstance(response, HttpResponse):
            raise TransportError("network", "network")
        return response
