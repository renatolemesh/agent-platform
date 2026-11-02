"""console-maintained tables: author, note, activation time on domain_datasets"""
from pathlib import Path

from alembic import op

revision = "0007_dataset_authoring"
down_revision = "0006_tenant_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    raw.execute((Path(__file__).resolve().parents[1] / "sql" / "0008_dataset_authoring.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("forward-only migrations (expand/contract)")
