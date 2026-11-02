"""business dashboard views"""
from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0004_dashboards.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
