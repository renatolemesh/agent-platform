"""This repository is public (ADR-023). What belongs to a client or to the owner stays out: this catches what can be recognized without naming it.
The names themselves are on a private list used by the commit hook (scripts/hooks/forbid_sensitive.sh)."""
from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_IP = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_DOC_RANGES = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def _tracked() -> list[str]:
    return subprocess.run(["git", "ls-files"], capture_output=True, text=True, encoding="utf-8", cwd=ROOT, check=True).stdout.splitlines()


def test_nothing_private_is_tracked():
    private = ("docs/privado/", "docs/materiais/", "docker/tenants/", "docker/materiais/")
    assert not [f for f in _tracked() if f.startswith(private) or f.lower().endswith(".pdf") or f.endswith("/knowledge_inventory.yaml")]


def test_only_the_example_and_dev_tenants_are_tracked():
    tenants = [f for f in _tracked() if f.startswith("config/tenants/")]
    assert tenants and all(f.endswith(("/dev-consortium.yaml", ".example.yaml")) for f in tenants)


def test_no_public_address_of_a_real_server():
    found = []
    for f in _tracked():
        path = ROOT / f
        if f.endswith(".lock") or not path.exists():
            continue
        for ip in _IP.findall(path.read_text(encoding="utf-8", errors="ignore")):
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.is_global and not any(addr in n for n in _DOC_RANGES):
                found.append(f"{f}: {ip}")
    assert not found, found


def test_the_address_check_itself_works():
    assert _IP.findall("203.0.113.4 x 10.0.0.1") == ["203.0.113.4", "10.0.0.1"]
