"""Project root resolution: repo checkout in dev, AGENTPLATFORM_ROOT (e.g. /app) inside the container."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("AGENTPLATFORM_ROOT") or Path(__file__).resolve().parents[2])
PROMPTS = ROOT / "prompts"
CONFIG = ROOT / "config"
EVALS = ROOT / "evals"
