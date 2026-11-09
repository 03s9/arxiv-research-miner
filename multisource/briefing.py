"""Analyst briefing: short context, one feature block, no abstract and no PDF bytes."""

from __future__ import annotations

from multisource.practical import Decision


def _short(text: str, words: int = 8) -> str:
    parts = (text or "").split()
    if len(parts) <= words:
        return text.strip()
    return " ".join(parts[:words])


def render_briefing(items: list[dict[str, object]]) -> str:
    accepted = [
        item
        for item in items
        if isinstance(item.get("practical"), Decision)
        and item["practical"].recommendation == "accepted_on_supplied_evidence"
    ]
    lines = [
        f"Context: {len(items)} artigo(s) normalizado(s); "
        f"{len(accepted)} recomendação prática aceita por evidência fornecida.",
        "",
        "**MELHORIA / ALTERAÇÃO / FEATURE**",
        "",
    ]
    if not accepted:
        lines.append(
            "Nenhuma melhoria prática confirmada. Artigo teórico, abstract ou link de "
            "repositório não vira recomendação. Evidência de autor sem reprodução "
            "independente permanece não aceita. Este processo não executa código de terceiros "
            "e não certifica que algo está em execução."
        )
    else:
        for index, item in enumerate(accepted, start=1):
            decision = item["practical"]
            assert isinstance(decision, Decision)
            detail = str(item.get("evidence_detail") or "").strip()
            title = _short(str(item.get("title") or "sem título"))
            lines.append(
                f"{index}. {title}: {detail} "
                "(independent_reproduction is not production_verified; "
                "production_verified=false; certifies_running=false)."
            )
    lines.extend(["", "Evidence:"])
    if not items:
        lines.append("- nenhum artigo")
    for item in items:
        decision = item.get("practical")
        reason = decision.reason if isinstance(decision, Decision) else "unknown"
        recommendation = decision.recommendation if isinstance(decision, Decision) else "unknown"
        lines.append(
            f"- {item.get('provider')} {item.get('link')} "
            f"recommendation={recommendation} reason={reason} "
            f"production_state={item.get('production_state')} "
            f"production_verified=false "
            f"independent_reproduction={item.get('independent_reproduction')} "
            "certifies_running=false"
        )
    lines.extend(["", "PDF: não incluído. URLs, se houver, ficam no manifesto e no JSON de papers.", ""])
    return "\n".join(lines)
