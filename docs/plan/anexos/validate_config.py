"""Validates config.example.yaml and checks that unsafe configs are rejected. Run: python validate_config.py"""
import sys, yaml, copy, pathlib
HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
from config_schema import PlatformConfig, ComplianceConfig, validate_against_package
data = yaml.safe_load(open(HERE / "config.example.yaml", encoding="utf-8"))
cfg = PlatformConfig.model_validate(data)
print("OK example validates; models.respond =", cfg.models.respond.primary)
pkg = ComplianceConfig(forbidden_phrases=cfg.compliance.forbidden_phrases, required_disclosures=cfg.compliance.required_disclosures)
validate_against_package(cfg, pkg); print("OK compliance equal to package accepted")
loose = copy.deepcopy(data); loose["compliance"]["forbidden_phrases"] = loose["compliance"]["forbidden_phrases"][:-2]
try:
    validate_against_package(PlatformConfig.model_validate(loose), pkg); print("FAIL: looser compliance accepted")
except ValueError as e: print("OK looser compliance rejected:", e)
bad = copy.deepcopy(data); bad["models"]["output_judge"]["primary"] = "deepseek/deepseek-v4.1-flash"
try: PlatformConfig.model_validate(bad); print("FAIL: same-family judge accepted")
except Exception as e: print("OK same-family judge rejected")
bad2 = copy.deepcopy(data); bad2["budget"]["daily_cap_usd"] = "3.00"
try: PlatformConfig.model_validate(bad2); print("FAIL: cap above OR lock accepted")
except Exception: print("OK daily cap above OpenRouter lock rejected")
bad3 = copy.deepcopy(data); bad3["models"]["respond"]["provider"]["data_collection"] = "allow"
try: PlatformConfig.model_validate(bad3); print("FAIL: data_collection allow accepted")
except Exception: print("OK data_collection=allow rejected")
bad4 = copy.deepcopy(data); bad4["comments"]["rules"][0]["public_variants"] = ["só uma"]
try: PlatformConfig.model_validate(bad4); print("FAIL: single public variant accepted")
except Exception: print("OK single public variant rejected")
bad5 = copy.deepcopy(data); bad5["unknown_key"] = 1
try: PlatformConfig.model_validate(bad5); print("FAIL: unknown key accepted")
except Exception: print("OK unknown key rejected")
import json; open(HERE / "config.schema.json","w",encoding="utf-8").write(json.dumps(PlatformConfig.model_json_schema(), indent=1, ensure_ascii=False))
print("JSON Schema exported")
bad6 = copy.deepcopy(data); bad6["models"]["output_judge"]["fallbacks"] = ["deepseek/deepseek-v4.1-flash"]
try: PlatformConfig.model_validate(bad6); print("FAIL: judge fallback same family accepted")
except Exception: print("OK judge fallback of respond family rejected")
bad7 = copy.deepcopy(data); bad7["budget"]["pilot_total_cap_usd"] = "5.00"
try: PlatformConfig.model_validate(bad7); print("FAIL: pilot cap above net credit accepted")
except Exception: print("OK pilot cap above net credit rejected")
bad8 = copy.deepcopy(data); bad8["models"]["embedding"]["provider"] = {"data_collection": "allow"}
try: PlatformConfig.model_validate(bad8); print("FAIL: embedding without ZDR accepted")
except Exception: print("OK embedding data_collection=allow rejected")
blank = copy.deepcopy(data); blank["compliance"]["required_disclosures"]["simulation_estimate"] = ""
try:
    validate_against_package(PlatformConfig.model_validate(blank), pkg); print("FAIL: blank disclosure accepted")
except ValueError: print("OK blanked disclosure rejected")
golive = copy.deepcopy(data); golive["operations"]["feature_flags"]["go_live"] = True
try: PlatformConfig.model_validate(golive); print("FAIL: go_live with pending transfer accepted")
except Exception: print("OK go_live blocked while subprocessor transfer is pending")
