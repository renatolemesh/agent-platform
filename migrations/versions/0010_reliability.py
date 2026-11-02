"""seller notices, owner escalation, and no clear text left in the inbound event log"""
from pathlib import Path

from alembic import op

revision = "0010_reliability"
down_revision = "0009_dm_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0011_reliability.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
