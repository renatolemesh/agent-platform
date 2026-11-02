"""simulator: keep simulated leads out of the business dashboards"""
from pathlib import Path

from alembic import op

revision = "0005_simulator_views"
down_revision = "0004_console"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0006_simulator_views.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
