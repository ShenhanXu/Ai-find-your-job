import pytest

from ingestion.ratelimit import HostRateLimiter


pytest.importorskip("lupa", reason="token bucket Lua tests need fakeredis[lua]")


def test_burst_then_throttle(redis_client):
    limiter = HostRateLimiter(redis_client, rate_per_second=1.0, burst=2)

    assert limiter.wait_time_ms("api.example.com") == 0
    assert limiter.wait_time_ms("api.example.com") == 0
    # Bucket empty: third request must wait for a refill.
    assert limiter.wait_time_ms("api.example.com") > 0


def test_hosts_are_isolated(redis_client):
    limiter = HostRateLimiter(redis_client, rate_per_second=1.0, burst=1)

    assert limiter.wait_time_ms("a.example.com") == 0
    assert limiter.wait_time_ms("a.example.com") > 0
    assert limiter.wait_time_ms("b.example.com") == 0


def test_wait_time_reflects_refill_rate(redis_client):
    slow = HostRateLimiter(redis_client, rate_per_second=0.5, burst=1, prefix="slow:")
    fast = HostRateLimiter(redis_client, rate_per_second=10.0, burst=1, prefix="fast:")

    slow.wait_time_ms("h")
    fast.wait_time_ms("h")
    assert slow.wait_time_ms("h") > fast.wait_time_ms("h")
