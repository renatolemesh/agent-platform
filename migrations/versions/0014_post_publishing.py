"""posts written in the console: drafts, encrypted files until published, comments on or off, removed posts"""
from pathlib import Path

from alembic import op

revision = "0014_post_publishing"
down_revision = "0013_config_console"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0015_post_publishing.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
