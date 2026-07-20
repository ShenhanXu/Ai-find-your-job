from datetime import datetime, timezone
from typing import Any


COUNTERS_KEY = "ingest:counters"


def incr(redis: Any, field: str, amount: int = 1) -> None:
    redis.hincrby(COUNTERS_KEY, field, amount)


def mark_activity(redis: Any, field: str) -> None:
    redis.hset(COUNTERS_KEY, f"{field}_at", datetime.now(timezone.utc).isoformat())


def snapshot(redis: Any) -> dict[str, str]:
    return redis.hgetall(COUNTERS_KEY) or {}
