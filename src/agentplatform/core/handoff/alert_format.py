"""What a seller alert says, in words a person reads on the phone: the reason and stage by name, the profile with the package's
labels, and whether the customer left a WhatsApp (never the number). Segment words come from the package, never from here."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# Handoff reasons as the console shows them (ui.REASON is this same map)
REASON = {"knowledge_gap": "Pergunta sem resposta aprovada", "opt_out": "Pediu para parar", "opted_out_returned": "Pediu para parar e voltou a escrever",
          "technical_failure": "Falha técnica", "conversation_budget": "Teto desta conversa",
          "human_unanswered": "Cliente sem resposta",
          "explicit_request": "Pediu um vendedor", "hot_lead": "Lead quente", "frustration": "Cliente irritado",
          "no_progress": "Conversa sem avanço", "guard_blocked": "Resposta bloqueada pela revisão", "media_review": "Mídia para revisar",
          "complaint_or_abuse": "Reclamação", "legal": "Assunto jurídico", "requires_sensitive_data": "Exige dado sensível",
          "simulation_request": "Pediu simulação", "specialist_question": "Pergunta para o vendedor", "post_sale": "Pós-venda", "cancellation": "Cancelamento",
          "out_of_scope": "Fora do escopo", "budget_exhausted": "Orçamento de IA esgotado"}


def reason_label(code: str) -> str:
    return REASON.get(code, code.replace("_", " ").capitalize())


def stage_label(stage: str | None, manifest=None) -> str | None:
    if not stage:
        return None
    for st in getattr(manifest, "stages", None) or []:
        if st.name == stage and st.label:
            return st.label
    return stage.replace("_", " ").capitalize()


def _money(v) -> str:
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return str(v)
    whole = f"{int(d):,}".replace(",", ".")
    cents = abs(d - int(d))
    return f"R$ {whole}" if cents == 0 else f"R$ {whole},{int(cents * 100):02d}"


def _value(spec, v) -> str:
    if spec is None:
        return str(v)
    if spec.type == "money":
        return _money(v)
    if spec.type == "bool":
        return "Sim" if str(v).lower() in ("true", "1", "sim", "yes") else "Não"
    if spec.type == "enum":
        return spec.value_labels.get(str(v), str(v).replace("_", " "))
    return str(v)


def profile_lines(summary: dict, manifest=None) -> list[str]:
    """One "Label: value" per known slot, in the package's slot order."""
    prof = {k: v for k, v in (summary.get("profile") or {}).items() if v not in (None, "")}
    specs = {s.name: s for s in getattr(manifest, "slots", None) or []}
    order = [n for n in specs if n in prof] + [n for n in prof if n not in specs]
    return [f"{(specs[n].label if n in specs and specs[n].label else n.replace('_', ' ').capitalize())}: {_value(specs.get(n), prof[n])}"
            for n in order]


def alert_details(summary: dict, manifest=None) -> tuple[str, ...]:
    lines = profile_lines(summary, manifest)
    if summary.get("whatsapp_left"):
        lines.append("📱 Deixou o WhatsApp: o número está na conversa")       # the number itself stays in the console
    if summary.get("last_message"):
        lines.append(f"💬 Última mensagem: “{summary['last_message'][:200]}”")
    return tuple(lines)


def summary_line(summary: dict, manifest=None) -> str:
    """The same, in one line, for channels with a single field (the WhatsApp template, the e-mail summary)."""
    return " · ".join(alert_details(summary, manifest))[:300]
