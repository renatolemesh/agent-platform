"""USD -> BRL for display only (budgets, caps and the provider bill stay in USD). Source: Banco Central PTAX (sell rate
of the latest business day), cached in memory and refreshed every few hours; FX_FALLBACK_USD_BRL is used until the first
successful fetch or when FX_SOURCE=fixed. Never used for any limit or decision."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import httpx

from agentplatform.settings import get_settings

log = logging.getLogger(__name__)

PTAX_URL = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
            "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)")
REFRESH_S = 6 * 3600


@dataclass
class Rate:
    value: Decimal
    source: str            # "PTAX dd/mm/aaaa" or "fixa"
    fetched_at: float


_cache: Rate | None = None
_refreshing = False


def current() -> Rate:
    """Cached rate (sync, safe inside template filters). Schedules a background refresh when stale."""
    s = get_settings()
    fixed = Rate(Decimal(str(s.fx_fallback_usd_brl)), "fixa", 0.0)
    if s.fx_source != "ptax":
        return fixed
    if _cache is None or time.time() - _cache.fetched_at > REFRESH_S:
        _schedule_refresh()
    return _cache or fixed


def _schedule_refresh() -> None:
    global _refreshing
    if _refreshing:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _refreshing = True
    loop.create_task(refresh())


async def refresh(transport: httpx.AsyncBaseTransport | None = None) -> Rate | None:
    global _cache, _refreshing
    today = date.today()
    # The OData service needs literal "@" and "$" in the parameter names (httpx would percent-encode them).
    url = (f"{PTAX_URL}?@dataInicial='{(today - timedelta(days=10)):%m-%d-%Y}'&@dataFinalCotacao='{today:%m-%d-%Y}'"
           "&$format=json&$orderby=dataHoraCotacao%20desc&$top=1")
    try:
        async with httpx.AsyncClient(timeout=5, transport=transport) as c:
            r = await c.get(httpx.URL(url))
            r.raise_for_status()
            row = r.json()["value"][0]
        day = row["dataHoraCotacao"][:10]
        _cache = Rate(Decimal(str(row["cotacaoVenda"])), f"PTAX {day[8:10]}/{day[5:7]}/{day[:4]}", time.time())
        return _cache
    except Exception as e:
        log.warning("PTAX fetch failed (%s); using the fallback USD/BRL rate", type(e).__name__)
        return None
    finally:
        _refreshing = False


def to_brl(usd) -> Decimal:
    return Decimal(str(usd or 0)) * current().value
