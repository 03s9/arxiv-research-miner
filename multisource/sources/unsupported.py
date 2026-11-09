"""Sources with no official public route. They do not pretend to return papers."""

from __future__ import annotations

from multisource.models import SourceOutcome

SCHOLAR_REASON = (
    "unsupported: Google Scholar has no official public API; "
    "automated access is disallowed and blocked by captcha. This tool does not scrape it."
)
PWC_REASON = (
    "unsupported: the official Papers with Code API at paperswithcode.com/api/v1 "
    "was discontinued when the service was sunset in July 2025. "
    "A direct request can redirect to https://huggingface.co/papers/trending. "
    "That historical redirect is not a Hugging Face collector; this tool does not call huggingface.co."
)


class GoogleScholarSource:
    provider = "google_scholar"

    def collect(self) -> SourceOutcome:
        return SourceOutcome(self.provider, "unsupported", SCHOLAR_REASON, None, "not_used")


class PapersWithCodeSource:
    provider = "papers_with_code"

    def collect(self) -> SourceOutcome:
        return SourceOutcome(self.provider, "unsupported", PWC_REASON, None, "not_used")
