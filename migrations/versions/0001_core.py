"""core schema + rls"""
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for name in ("0001_core.sql", "0002_rls_roles.sql"):
        # raw driver call without params: psycopg must not parse % placeholders inside format() strings
        raw = op.get_bind().connection.driver_connection
        raw.execute((SQL / name).read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
