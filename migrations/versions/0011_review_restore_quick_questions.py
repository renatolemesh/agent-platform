"""findings of the daily review, restore test history, quick questions at the start of a direct"""
from pathlib import Path

from alembic import op

revision = "0011_review_restore_quick"
down_revision = "0010_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0012_review_restore_quick_questions.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
