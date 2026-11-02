"""The real app in its own process, for the browser tests. Own process because the app keeps one database pool on one event loop."""
import asyncio
import sys

import uvicorn

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from agentplatform.api.main import app  # noqa: E402

asyncio.run(uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")).serve())
