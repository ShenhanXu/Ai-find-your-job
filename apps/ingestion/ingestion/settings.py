import os


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def get_redis():
    import redis

    return redis.Redis.from_url(redis_url(), decode_responses=True)


SCHEDULER_TICK_SECONDS = int_env("SCHEDULER_TICK_SECONDS", 60)
CRAWL_CONCURRENCY = int_env("CRAWL_CONCURRENCY", 4)
CRAWL_ATS_TYPES = tuple(
    part.strip() for part in os.getenv("CRAWL_ATS_TYPES", "greenhouse,lever").split(",") if part.strip()
)
RATE_LIMIT_PER_HOST = float_env("RATE_LIMIT_PER_HOST", 1.0)
RATE_LIMIT_BURST = int_env("RATE_LIMIT_BURST", 2)
MAX_DELIVERIES = int_env("MAX_DELIVERIES", 5)
# Must exceed the longest legitimate in-flight crawl (Retry-After up to 120s plus
# rate-limit waits), or peers XAUTOCLAIM healthy tasks and double-crawl them.
CLAIM_IDLE_MS = int_env("CLAIM_IDLE_MS", 300_000)
CONSUMER_BLOCK_MS = int_env("CONSUMER_BLOCK_MS", 5_000)
CONSUMER_BATCH = int_env("CONSUMER_BATCH", 16)
EMBED_BATCH_SIZE = int_env("EMBED_BATCH_SIZE", 64)
# Pause between embed batches; tune together with EMBED_BATCH_SIZE to stay under
# the provider's tokens-per-minute cap (Jina free tier: ~100k/min).
EMBED_PAUSE_SECONDS = float_env("EMBED_PAUSE_SECONDS", 0.0)
STREAM_MAXLEN = int_env("STREAM_MAXLEN", 100_000)
STALE_CRAWL_CYCLES = int_env("STALE_CRAWL_CYCLES", 3)
EMBED_DEDUP_TTL_SECONDS = int_env("EMBED_DEDUP_TTL_SECONDS", 6 * 3600)
