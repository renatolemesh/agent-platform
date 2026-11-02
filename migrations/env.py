"""Alembic env: migrations are plain SQL files executed with the owner role (MIGRATION_DATABASE_URL)."""
from alembic import context
from sqlalchemy import create_engine

from agentplatform.settings import get_settings


def run() -> None:
    url = get_settings().migration_database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


run()
