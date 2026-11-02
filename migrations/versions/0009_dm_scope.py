"""direct scope: who the assistant may serve, and contacts it must leave to the account owner"""
from pathlib import Path

from alembic import op

revision = "0009_dm_scope"
down_revision = "0008_comment_automation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0010_dm_scope.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
