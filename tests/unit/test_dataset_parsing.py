"""Console tables accept what people type or export from spreadsheets: Brazilian numbers, labels instead of codes."""
from decimal import Decimal

from agentplatform.core.knowledge.datasets import parse_number, to_csv, validate_rows
from agentplatform_packages.consortium import PlanRow, installment


def test_parse_number_brazilian_formats():
    cases = {"R$ 300.000,00": "300000.00", "1.500": "1500", "0.05": "0.05", "0,05": "0.05", "24%": "24",
             "14,5": "14.5", "1,546.67": "1546.67", "80000": "80000", 60.0: "60", 0.24: "0.24", "": None}
    for raw, want in cases.items():
        assert parse_number(raw) == want, raw


def test_labels_choices_and_errors_in_portuguese():
    rows = [{"Código": "A", "Objetivo": "carro ou moto", "Crédito (R$)": "80.000", "Prazo (meses)": "60,0",
             "Taxa de administração total (%)": "14"},
            {"plan_id": "B", "goal": "foguete", "credit": "-1", "term_months": "60"}]
    valid, errors, ignored = validate_rows(rows, PlanRow, key="plan_id")
    assert valid[0]["goal"] == "vehicle" and valid[0]["term_months"] == 60 and valid[0]["credit"] == "80000"
    assert len(errors) == 1 and errors[0].startswith("linha 3: Objetivo: use uma das opções: Imóvel")
    assert "Crédito (R$): deve ser maior que 0" in errors[0] and not ignored


def test_official_installment_wins_over_formula():
    row = PlanRow(plan_id="X", goal="vehicle", credit=Decimal(80000), term_months=60, admin_fee_pct=Decimal(14),
                  installment=Decimal("1499.90"))
    assert installment(row)[0] == Decimal("1499.90")


def test_export_round_trips():
    valid, _, _ = validate_rows([{"plan_id": "A", "goal": "real_estate", "credit": "300000.50", "term_months": "200",
                                  "admin_fee_pct": "20"}], PlanRow)
    text = to_csv(valid, PlanRow).decode("utf-8-sig")
    lines = [ln.split(";") for ln in text.strip().splitlines()]
    again, errors, _ = validate_rows([dict(zip(lines[0], lines[1]))], PlanRow)
    assert not errors and again == valid and "Imóvel" in lines[1] and "300000,50" in lines[1]


def test_rows_with_official_and_reduced_installment():
    """A table can list credit -> installment (and a reduced one until contemplation), without term or fee."""
    from agentplatform_packages.consortium import SimulateIn, simulate
    valid, errors, _ = validate_rows([
        {"Código": "I100", "Objetivo": "Imóvel", "Crédito (R$)": "100.000,00", "Parcela oficial (R$)": "800,00",
         "Parcela reduzida até a contemplação (R$)": "500,00"},
        {"Código": "I200", "Objetivo": "Imóvel", "Crédito (R$)": "200.000,00", "Parcela oficial (R$)": "1.600,00"},
        {"plan_id": "BAD", "goal": "vehicle", "credit": "90000"},
        {"plan_id": "BAD2", "goal": "vehicle", "credit": "90000", "installment": "500", "reduced_installment": "600"}],
        PlanRow, key="plan_id")
    assert [v["plan_id"] for v in valid] == ["I100", "I200"]
    assert errors == ["linha 4: informe a parcela oficial ou o prazo e a taxa de administração",
                      "linha 5: a parcela reduzida deve ser menor que a parcela oficial"]
    rows = [PlanRow.model_validate(v) for v in valid]
    out = simulate(rows, SimulateIn(goal="real_estate", monthly_budget=Decimal(600), term_months=200), "d", 1)
    assert [o.plan_id for o in out.options] == ["I100"]          # fits by the reduced installment, term unknown is kept
    assert out.options[0].installment == Decimal("800.00") and out.options[0].reduced_installment == Decimal("500.00")


def test_simulation_without_goal_or_term_offers_the_cheapest_entry():
    """Amount only ("uma carta de 100 mil"): every goal, the closest credit, then the lowest entry. The offer carries both installments."""
    from agentplatform_packages.consortium import SimulateIn, simulate
    rows = [PlanRow(plan_id="A100", goal="vehicle", credit=Decimal(100000), installment=Decimal("1750.00")),
            PlanRow(plan_id="I100", goal="real_estate", credit=Decimal(100000), installment=Decimal("800.00"),
                    reduced_installment=Decimal("397.00")),
            PlanRow(plan_id="I300", goal="real_estate", credit=Decimal(300000), installment=Decimal("2400.00"))]
    out = simulate(rows, SimulateIn(credit_amount=Decimal(100000)), "d", 1)
    first = out.options[0]
    assert [o.plan_id for o in out.options] == ["I100", "A100", "I300"]
    assert first.goal == "Imóvel" and first.entry_installment == Decimal("397.00")
    assert out.required_text == "Pra uma carta de R$ 100.000,00 para imóvel, tem grupos com parcela a partir de R$ 397,00 até a contemplação."
    assert out.closing_question == "Faz sentido pra você?"
    term = PlanRow(plan_id="T", goal="vehicle", credit=Decimal(50000), term_months=60, installment=Decimal(900), insurance_monthly_pct=Decimal("0.1"))
    offer = simulate([term], SimulateIn(goal="vehicle"), "d", 1).required_text
    assert offer == "Pra uma carta de R$ 50.000,00 para carro ou moto, tem grupos com parcela a partir de R$ 900,00, mais seguro de R$ 50,00 por mês."
    assert "para" not in simulate([term], SimulateIn(goal="resale"), "d", 1).required_text    # a letter to sell later has no goal of its own


def test_the_estimate_starts_from_the_lowest_installment_for_that_credit():
    from agentplatform_packages.consortium import SimulateIn, simulate
    rows = [PlanRow(plan_id="A", goal="vehicle", credit=Decimal(100000), installment=Decimal("1750.00")),
            PlanRow(plan_id="B", goal="vehicle", credit=Decimal(100000), installment=Decimal("1500.00"), reduced_installment=Decimal("900.00")),
            PlanRow(plan_id="C", goal="vehicle", credit=Decimal(60000), installment=Decimal("700.00"))]
    out = simulate(rows, SimulateIn(goal="vehicle", credit_amount=Decimal(100000)), "d", 1)
    assert out.required_text == "Pra uma carta de R$ 100.000,00 para carro ou moto, tem grupos com parcela a partir de R$ 900,00 até a contemplação."
    assert "700" not in out.required_text                      # a smaller credit is not what was asked


def test_the_group_average_goes_in_the_offer_only_from_the_table_and_always_as_history():
    from agentplatform_packages.consortium import MANIFEST, SimulateIn, simulate
    note = MANIFEST.compliance.required_disclosures["history_not_guarantee"]
    with_avg = PlanRow(plan_id="G", goal="vehicle", credit=Decimal(95000), term_months=72, installment=Decimal(980), avg_contemplation_months=18)
    out = simulate([with_avg], SimulateIn(goal="vehicle"), "d", 1)
    assert out.required_text.endswith(f"a partir de R$ 980,00. Nesse grupo, as contemplações têm saído em média em 18 meses. {note}")
    assert out.disclaimer_keys == ["history_not_guarantee"]
    plain = simulate([with_avg.model_copy(update={"avg_contemplation_months": None})], SimulateIn(goal="vehicle"), "d", 1)
    assert "contemplações" not in plain.required_text and plain.disclaimer_keys == []
    empty = simulate([], SimulateIn(), "d", 1)
    assert empty.required_text is None and empty.closing_question is None
