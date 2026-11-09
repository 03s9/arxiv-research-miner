"""Map HTTP results to distinct source statuses. Bodies are not copied into errors."""

from __future__ import annotations

import json
from typing import Any

from multisource.http_client import (
    BlockedUrl,
    HttpResponse,
    OfflineError,
    TransportError,
    rate_limited,
)


def failure_status(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, OfflineError):
        return "upstream_error", "network_disabled"
    if isinstance(exc, BlockedUrl):
        return "blocked_url", exc.reason
    if isinstance(exc, TransportError):
        if exc.kind == "timeout":
            return "timeout", "timeout"
        return "upstream_error", exc.reason
    return "upstream_error", "network"


def http_failure(response: HttpResponse, *, key_provided: bool) -> str | None:
    if rate_limited(response):
        return "rate_limited"
    if response.status == 401:
        return "rejected_credentials" if key_provided else "missing_credentials"
    if response.status == 403:
        sample = response.body[:400].lower()
        if b"api key" in sample or b"unauthorized" in sample or b"bad credentials" in sample:
            return "rejected_credentials" if key_provided else "missing_credentials"
        return "upstream_error"
    if response.status == 404:
        return "not_found"
    if response.status != 200:
        return "upstream_error"
    return None


def json_payload(response: HttpResponse) -> Any:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
