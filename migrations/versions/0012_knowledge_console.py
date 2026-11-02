"""material managed by a person in the console: upload, review, approval, FAQs"""
from pathlib import Path

from alembic import op

revision = "0012_knowledge_console"
down_revision = "0011_review_restore_quick"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0013_knowledge_console.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
