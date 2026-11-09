"""Allowlisted provider HTTP and public-document checks. No open crawl."""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
import urllib.parse
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


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")
_6TO4 = ipaddress.ip_network("2002::/16")


def _ipv4_unsafe(address: ipaddress.IPv4Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


def address_is_unsafe(text: str) -> bool:
    """Reject private, metadata, NAT64, and deprecated IPv4-compatible literals.

    ipaddress.is_global is not enough: 64:ff9b::/96 and ::/96 can embed those
    targets and still report is_global True. This predicate does not open a socket.
    """
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return _ipv4_unsafe(address)
    if address in _NAT64 or address in _NAT64_LOCAL or address in _V4_COMPATIBLE:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None and _ipv4_unsafe(mapped):
        return True
    if address in _6TO4:
        embedded = ipaddress.IPv4Address((int(address) >> 80) & 0xFFFFFFFF)
        if _ipv4_unsafe(embedded):
            return True
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


def _non_public_ip(text: str) -> bool:
    return address_is_unsafe(text)


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


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to one already-checked IP. The TCP peer is never chosen by a second lookup."""

    def __init__(self, host: str, ip: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, port=443, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class UrllibTransport:
    def __init__(
        self,
        resolver: Resolver | None = None,
        connector: Callable[..., HttpResponse] | None = None,
    ) -> None:
        self.resolver = resolver or default_resolver
        self.connector = connector

    def send(self, request: HttpRequest) -> HttpResponse:
        if os.environ.get("MULTISOURCE_OFFLINE") == "1":
            raise OfflineError("network disabled")
        host, chosen = self._pin(request.url)
        if self.connector is not None:
            return self.connector(chosen, host, request)
        return self._pinned_request(chosen, host, request)

    def _pin(self, url: str) -> tuple[str, str]:
        parts, host = _split_https(url)
        del parts
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if literal:
            if address_is_unsafe(host):
                raise BlockedUrl("non_public_ip")
            return host, host
        addresses = self.resolver(host)
        if not addresses:
            raise BlockedUrl("dns_failed")
        for item in addresses:
            if address_is_unsafe(item):
                raise BlockedUrl("non_public_ip")
        # Any later name lookup is intentionally not used. Limits: one address,
        # no alternate-address retry, certificate is checked for the hostname.
        return host, addresses[0]

    def _pinned_request(self, ip: str, host: str, request: HttpRequest) -> HttpResponse:
        context = ssl.create_default_context()
        connection = _PinnedHTTPSConnection(host, ip, request.timeout, context)
        target = urllib.parse.urlsplit(request.url)
        path = target.path or "/"
        if target.query:
            path = f"{path}?{target.query}"
        headers = {key: value for key, value in request.headers.items() if key.lower() != "host"}
        try:
            connection.request(request.method, path, headers=headers)
            response = connection.getresponse()
            body = response.read(MAX_BYTES + 1)
            header_map = {key.lower(): value for key, value in response.getheaders()}
            return HttpResponse(int(response.status), request.url, header_map, body)
        except (TimeoutError, socket.timeout):
            raise TransportError("timeout", "timeout") from None
        except ssl.SSLError:
            raise TransportError("network", "tls") from None
        except OSError:
            raise TransportError("network", "network") from None
        finally:
            connection.close()


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
            nxt = urljoin(current, location)
            current_host = (urlsplit(current).hostname or "").lower()
            next_host = (urlsplit(nxt).hostname or "").lower()
            if next_host and next_host != current_host:
                headers = {
                    key: value
                    for key, value in headers.items()
                    if key.lower() not in {"authorization", "x-api-key"}
                }
            current = nxt
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
