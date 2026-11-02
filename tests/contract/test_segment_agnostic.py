"""Plan acceptance (13 §1): the core never mentions a segment, and the SAME generic suite passes for the consortium
package and the fictitious auto_insurance package."""
import re
from pathlib import Path

import pytest

from agentplatform.core.agent import policy
from agentplatform.core.agent.guards import OutputContext, failed, output_guard
from agentplatform.core.agent.state import SlotUpdate, StageProposal, TurnUnderstanding
from agentplatform.core.config.schema import FunnelConfig
from agentplatform.core.packages.contract import load_package

SRC = Path(__file__).resolve().parents[2] / "src" / "agentplatform"
FIXED_TERMS = [r"cons[oó]rci", r"contempla", r"\blances?\b", r"\bcotas?\b", r"assembleia", r"prestamista",
               r"carta de cr[eé]dito"]


def test_core_is_segment_agnostic():
    glossary = []
    for name in ("consortium",):
        glossary += [r"\b" + re.escape(k) + r"\b" for k in load_package(name).manifest.glossary]
    patterns = [re.compile(p, re.IGNORECASE) for p in FIXED_TERMS + glossary]
    hits = []
    for f in SRC.rglob("*"):
        if f.suffix not in (".py", ".html", ".j2") or "lab" in f.parts:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in patterns):
                hits.append(f"{f.relative_to(SRC)}:{i}: {line.strip()[:80]}")
    assert not hits, "segment terms in core:\n" + "\n".join(hits)


@pytest.fixture(params=["consortium", "auto_insurance"])
def pkg(request):
    return load_package(request.param)


def U(**kw):
    base = dict(intents=[], questions=[], slot_updates=[], objections=[], sentiment="neutral", asks_for_human=False,
                out_of_scope=False, opt_out_intent=0.0)
    base.update(kw)
    return TurnUnderstanding(**base)


def test_manifest_consistency(pkg):
    m = pkg.manifest
    assert m.allowed(m.initial_stage, m.initial_stage) is None
    assert {m.initial_stage, m.form_stage, m.seller_stage, m.won_stage} <= {s.name for s in m.stages}
    assert m.stage(m.won_stage).marked_by_seller_only


def test_only_declared_transitions(pkg):
    m = pkg.manifest
    terminal = m.won_stage
    d = policy.decide_stage(m, m.initial_stage, StageProposal(stage=terminal, confidence=1, evidence=["x"]), {}, FunnelConfig())
    assert not d.applied


def test_flow_and_min_profile(pkg):
    m = pkg.manifest
    q = policy.next_question(m, {})
    assert q                                                  # the flow asks something first
    full = {s: {"value": "1"} for group in m.min_profile_for_proposal for s in group[:1]}
    assert m.profile_complete(full)


def test_unknown_slots_rejected(pkg):
    m = pkg.manifest
    u = U(slot_updates=[SlotUpdate(slot="cpf", value="1", evidence_quote="oi", confidence=1, is_correction=False)])
    assert policy.apply_slots(m, {}, u, "oi", FunnelConfig(), None).rejected == [("cpf", "unknown_slot")]


def test_package_compliance_enforced(pkg):
    c = pkg.manifest.compliance
    ctx = OutputContext(c, [], policy.config_numbers(), [], 1000)
    assert any(v.verdict == "block" for v in failed(output_guard("Valor de 999 reais.", ctx)))   # numbers always need a source


def test_domain_tools_are_declared(pkg):
    for t in pkg.tools():
        assert t.effect_class in ("read", "internal_write", "external") and t.allowed_stages
