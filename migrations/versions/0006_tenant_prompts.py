"""prompt lab: tenant prompt versions in prompt_templates (RLS)"""
from pathlib import Path

from alembic import op

revision = "0006_tenant_prompts"
down_revision = "0005_simulator_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0007_tenant_prompts.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
