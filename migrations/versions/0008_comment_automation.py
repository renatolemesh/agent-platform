"""comment automation per post: media catalog, account default, per-post settings"""
from pathlib import Path

from alembic import op

revision = "0008_comment_automation"
down_revision = "0007_dataset_authoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0009_comment_automation.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
