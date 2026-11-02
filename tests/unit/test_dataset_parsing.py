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
    """Amount only ("uma carta de 100 mil"): every goal, the closest credit, then the lowest entry; approx never understates."""
    from agentplatform_packages.consortium import SimulateIn, approx_up, simulate
    rows = [PlanRow(plan_id="A100", goal="vehicle", credit=Decimal(100000), installment=Decimal("1750.00")),
            PlanRow(plan_id="I100", goal="real_estate", credit=Decimal(100000), installment=Decimal("800.00"),
                    reduced_installment=Decimal("397.00")),
            PlanRow(plan_id="I300", goal="real_estate", credit=Decimal(300000), installment=Decimal("2400.00"))]
    out = simulate(rows, SimulateIn(credit_amount=Decimal(100000)), "d", 1)
    first = out.options[0]
    assert [o.plan_id for o in out.options] == ["I100", "A100", "I300"]
    assert first.goal == "Imóvel" and first.entry_installment == Decimal("397.00") and first.entry_approx == Decimal(400)
    assert approx_up(Decimal("300.00")) == 300 and approx_up(Decimal("300.01")) == 310 and approx_up(Decimal("1750")) == 1750
