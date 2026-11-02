"""Token buckets per channel account and bucket type (plan 05 §3). Redis (shared) or local (tests / Redis down)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

# capacity, refill per second (with 20% safety margin from the Meta limits)
BUCKETS: dict[str, tuple[float, float]] = {
    "ig_text": (80, 80.0),                  # 100/s -> 80/s
    "ig_media": (8, 8.0),                   # 10/s  -> 8/s
    "ig_private_reply": (600, 600 / 3600),  # 750/h -> 600/h
    "ig_conversations_api": (1.6, 1.6),     # 2/s
    "contact_inbound": (20, 20 / 60),       # anti-abuse: 20 msgs/min per contact
    "web_form": (10, 10 / 60),              # public lead form: 10 requests/min per IP
}


class RateLimiter(Protocol):
    async def try_acquire(self, key: str, bucket: str, cost: float = 1.0) -> float:
        """Returns 0 if acquired, else seconds to wait."""


@dataclass
class _State:
    tokens: float
    ts: float


class LocalRateLimiter:
    def __init__(self, start_empty: bool = False):
        self._s: dict[str, _State] = {}
        self._start_empty = start_empty

    async def try_acquire(self, key: str, bucket: str, cost: float = 1.0) -> float:
        cap, rate = BUCKETS[bucket]
        k = f"{bucket}:{key}"
        now = time.monotonic()
        st = self._s.get(k) or _State(0.0 if self._start_empty else cap, now)
        st.tokens = min(cap, st.tokens + (now - st.ts) * rate)
        st.ts = now
        self._s[k] = st
        if st.tokens >= cost:
            st.tokens -= cost
            return 0.0
        return (cost - st.tokens) / rate


_LUA = """
local k = KEYS[1]
local cap, rate, cost, now = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local s = redis.call('HMGET', k, 't', 'ts')
local tokens = tonumber(s[1])
local ts = tonumber(s[2])
if tokens == nil then tokens = 0; ts = now end   -- start EMPTY after loss (conservative)
tokens = math.min(cap, tokens + (now - ts) * rate)
local wait = 0
if tokens >= cost then tokens = tokens - cost else wait = (cost - tokens) / rate end
redis.call('HSET', k, 't', tokens, 'ts', now)
redis.call('EXPIRE', k, 3600)
return tostring(wait)
"""


class RedisRateLimiter:
    def __init__(self, redis_client):
        self._r = redis_client
        self._script = redis_client.register_script(_LUA)
        self._fallback = LocalRateLimiter(start_empty=True)

    async def try_acquire(self, key: str, bucket: str, cost: float = 1.0) -> float:
        cap, rate = BUCKETS[bucket]
        try:
            wait = await self._script(keys=[f"rl:{bucket}:{key}"], args=[cap, rate, cost, time.time()])
            return float(wait)
        except Exception:
            return await self._fallback.try_acquire(key, bucket, cost)   # conservative local limiter
