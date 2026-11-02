"""configuration in the console: draft base, evaluations, invites, vectors per index, reindex, conflicts"""
from pathlib import Path

from alembic import op

revision = "0013_config_console"
down_revision = "0012_knowledge_console"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0014_config_console.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
