import asyncio
import os
import sys
import uuid

import pytest

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

os.environ.setdefault("APP_ENV", "dev")
os.environ["FX_SOURCE"] = "fixed"          # no network calls to Banco Central from tests
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8100"   # tests talk plain http (secure cookies would be dropped)
os.environ.pop("META_OAUTH_CONFIG_ID", None)


def _use_test_database() -> tuple[str, str, str]:
    """Tests never touch the database the app/worker uses: same server, database `<name>_test` (created and migrated
    on demand). Env vars override .env in pydantic-settings, so this must run before get_settings() is first called."""
    from urllib.parse import urlsplit, urlunsplit

    from agentplatform.settings import Settings
    base = Settings()

    def to_test(url: str) -> str:
        parts = urlsplit(url)
        name = parts.path.lstrip("/")
        return url if name.endswith("_test") else urlunsplit(parts._replace(path=f"/{name}_test"))
    os.environ["DATABASE_URL"] = to_test(base.database_url)
    os.environ["MIGRATION_DATABASE_URL"] = to_test(base.migration_database_url)
    return base.migration_database_url, os.environ["MIGRATION_DATABASE_URL"], urlsplit(os.environ["DATABASE_URL"]).path[1:]


_ADMIN_URL, _TEST_OWNER_URL, _TEST_DB = _use_test_database()


def _db_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(_ADMIN_URL, connect_timeout=2, autocommit=True) as conn:
            if not conn.execute("select 1 from pg_database where datname = %s", (_TEST_DB,)).fetchone():
                conn.execute(f'create database "{_TEST_DB}"')
        return True
    except Exception:
        return False


DB_UP = _db_available()


def pytest_sessionstart(session):
    if DB_UP:                       # idempotent and fast when already at head
        from agentplatform.cli import cmd_migrate
        cmd_migrate(None)


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="dev Postgres not running (docker compose --profile dev up)")
    for item in items:
        if "db" in item.keywords and not DB_UP:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _queue_connection_per_test():
    """The API process keeps one queue connection open on its single event loop; each test has its own loop, so none is carried over."""
    yield
    jobs = sys.modules.get("agentplatform.jobs.app")
    if jobs is not None:
        jobs.app.connector._async_pool = None
        jobs._open_lock = None


@pytest.fixture
async def db():
    from agentplatform.core.db.pool import close_pool, get_pool
    await get_pool()
    yield
    await close_pool()


@pytest.fixture
async def tenant(db):
    """A fresh tenant with one Instagram account, created through the platform role."""
    from agentplatform.core.db.pool import platform_tx
    tid = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute(
            "insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,%s,%s,%s)",
            (tid, f"t-{tid.hex[:8]}", "Test tenant", "consortium", "0.1.0"))
        await conn.execute(
            "insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,'instagram',%s,%s)",
            (tid, f"ig-{tid.hex[:10]}", f"page-{tid.hex[:10]}"))
    return tid
