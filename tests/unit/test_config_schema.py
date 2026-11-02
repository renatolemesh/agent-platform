"""Ports docs/plan/anexos/validate_config.py into the test suite (F1-CFG-01)."""
import copy
from pathlib import Path

import pytest
import yaml

from agentplatform.core.config.schema import ComplianceConfig, PlatformConfig, validate_against_package

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def data():
    return yaml.safe_load((ROOT / "docs/plan/anexos/config.example.yaml").read_text(encoding="utf-8"))


def _rejects(d):
    with pytest.raises(Exception):
        PlatformConfig.model_validate(d)


def test_example_and_dev_configs_validate(data):
    PlatformConfig.model_validate(data)
    PlatformConfig.model_validate(yaml.safe_load((ROOT / "config/tenants/dev-consortium.yaml").read_text(encoding="utf-8")))


@pytest.mark.parametrize("mutate", [
    lambda d: d["models"]["output_judge"].update(primary="deepseek/deepseek-v4.1-flash"),
    lambda d: d["models"]["output_judge"].update(fallbacks=["deepseek/x"]),
    lambda d: d["budget"].update(daily_cap_usd="3.00"),
    lambda d: d["budget"].update(pilot_total_cap_usd="5.00"),
    lambda d: d["models"]["respond"]["provider"].update(data_collection="allow"),
    lambda d: d["models"]["embedding"].update(provider={"data_collection": "allow"}),
    lambda d: d.update(unknown_key=1),
    lambda d: d["operations"]["feature_flags"].update(go_live=True),
    lambda d: d["models"].update(allow_same_family_judge=True),   # only allowed in dev
])
def test_unsafe_configs_rejected(data, mutate):
    d = copy.deepcopy(data)
    mutate(d)
    _rejects(d)


def test_compliance_monotonic(data):
    cfg = PlatformConfig.model_validate(data)
    pkg = ComplianceConfig(forbidden_phrases=cfg.compliance.forbidden_phrases,
                           required_disclosures=cfg.compliance.required_disclosures)
    validate_against_package(cfg, pkg)
    loose = copy.deepcopy(data)
    loose["compliance"]["forbidden_phrases"] = loose["compliance"]["forbidden_phrases"][:-1]
    with pytest.raises(ValueError):
        validate_against_package(PlatformConfig.model_validate(loose), pkg)
    blank = copy.deepcopy(data)
    blank["compliance"]["required_disclosures"]["simulation_estimate"] = ""
    with pytest.raises(ValueError):
        validate_against_package(PlatformConfig.model_validate(blank), pkg)
