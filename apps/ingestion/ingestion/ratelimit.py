import asyncio
import time
from typing import Any


TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then tokens = burst end
if ts == nil then ts = now_ms end

tokens = math.min(burst, tokens + (now_ms - ts) / 1000.0 * rate)

if tokens >= 1 then
  redis.call('HSET', key, 'tokens', tokens - 1, 'ts', now_ms)
  redis.call('PEXPIRE', key, 120000)
  return 0
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, 120000)
return math.ceil((1 - tokens) / rate * 1000)
"""


class HostRateLimiter:
    """Per-host token bucket shared across workers via Redis; the Lua script keeps refill+take atomic."""

    def __init__(self, redis: Any, rate_per_second: float = 1.0, burst: int = 2, prefix: str = "ingest:ratelimit:") -> None:
        self.rate = rate_per_second
        self.burst = burst
        self.prefix = prefix
        self._script = redis.register_script(TOKEN_BUCKET_LUA)

    def wait_time_ms(self, host: str) -> int:
        now_ms = int(time.time() * 1000)
        return int(self._script(keys=[f"{self.prefix}{host}"], args=[self.rate, self.burst, now_ms]))

    async def acquire(self, host: str, max_wait_seconds: float = 60.0) -> None:
        waited = 0.0
        while True:
            # Sync Redis EVALSHA off the event loop so a slow Redis can't stall other crawls.
            delay_ms = await asyncio.to_thread(self.wait_time_ms, host)
            if delay_ms <= 0:
                return
            if waited + delay_ms / 1000 > max_wait_seconds:
                raise TimeoutError(f"Rate limit wait for {host} exceeded {max_wait_seconds}s")
            await asyncio.sleep(delay_ms / 1000)
            waited += delay_ms / 1000
