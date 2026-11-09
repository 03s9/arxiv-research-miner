"""Practical-evidence gate.

production_verified and independent_reproduction are different facts.
This collector never sets production_verified: a supplied attestation, an
author claim, a repository link, or an independent reproduction does not
verify production usage and does not certify that code is running.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from multisource.models import Evidence

_IMPLEMENTATION = {"code", "release", "system_or_demo"}
_OUTPUT_STATES = {
    "not_production",
    "heuristic",
    "author_claim",
    "inspected_unverified",
    "attested_unverified",
}


@dataclass(frozen=True)
class Decision:
    recommendation: str
    reason: str
    production_state: str
    production_verified: bool
    independent_reproduction: bool
    production_label_rejected: bool
    certifies_running: bool = False

    def public_dict(self) -> dict[str, object]:
        return {
            "recommendation": self.recommendation,
            "reason": self.reason,
            "production_state": self.production_state,
            "production_verified": False,
            "independent_reproduction": self.independent_reproduction,
            "production_label_rejected": self.production_label_rejected,
            "certifies_running": False,
            "tcc_eligibility_decided_here": False,
            "production_activation": False,
        }


def enforce_production(item: Evidence) -> Evidence:
    rejected = item.production_label_rejected or item.production_state == "production_verified"
    if item.kind == "production_usage" and item.observation == "observed" and item.supplied_by_caller:
        return replace(
            item,
            production_state="attested_unverified",
            production_label_rejected=rejected,
        )
    if item.kind in {"repo_link", "abstract_claim"} or item.observation != "observed":
        state = "heuristic" if item.observation == "inferred" else "not_production"
        rejected = rejected or item.production_state not in {state, "not_production", "heuristic"}
        return replace(item, production_state=state, production_label_rejected=rejected)
    if item.demonstration == "author_demonstration":
        rejected = rejected or item.production_state not in {"author_claim", "not_production"}
        return replace(item, production_state="author_claim", production_label_rejected=rejected)
    if item.kind in _IMPLEMENTATION:
        rejected = rejected or (
            item.production_state not in _OUTPUT_STATES
            and item.production_state != "inspected_unverified"
        )
        if item.production_state == "production_verified":
            rejected = True
        return replace(
            item,
            production_state="inspected_unverified",
            production_label_rejected=rejected,
        )
    rejected = rejected or item.production_state == "production_verified"
    return replace(item, production_state="not_production", production_label_rejected=rejected)


def _aggregate(evidence: list[Evidence]) -> str:
    states = {item.production_state for item in evidence}
    for state in (
        "attested_unverified",
        "inspected_unverified",
        "author_claim",
        "heuristic",
        "not_production",
    ):
        if state in states:
            return state
    return "not_production"


def classify(evidence: list[Evidence]) -> tuple[Decision, list[Evidence]]:
    normalized = [enforce_production(item) for item in evidence]
    rejected = any(item.production_label_rejected for item in normalized)
    independent = any(
        item.kind == "practical_result"
        and item.observation == "observed"
        and item.demonstration == "independent_reproduction"
        for item in normalized
    )
    if not normalized or all(item.kind == "abstract_claim" for item in normalized):
        recommendation, reason = "excluded_theory_only", "no_implementation_evidence"
    elif all(item.kind in {"abstract_claim", "repo_link", "production_usage"} for item in normalized):
        recommendation, reason = "excluded_insufficient_evidence", "abstract_or_repo_link_only"
    else:
        observed_impl = [
            item
            for item in normalized
            if item.kind in _IMPLEMENTATION and item.observation == "observed"
        ]
        if not observed_impl:
            recommendation, reason = "withheld_unverified", "missing_observed_implementation"
        elif not independent:
            recommendation, reason = "withheld_unverified", "missing_independent_result"
        else:
            recommendation, reason = (
                "accepted_on_supplied_evidence",
                "accepted_supplied_independent_result",
            )
    if rejected:
        reason = f"{reason}:production_label_rejected"
    decision = Decision(
        recommendation=recommendation,
        reason=reason,
        production_state=_aggregate(normalized),
        production_verified=False,
        independent_reproduction=independent,
        production_label_rejected=rejected,
    )
    return decision, normalized
