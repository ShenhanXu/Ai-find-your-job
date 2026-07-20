import asyncio
import random
import urllib.parse
from typing import Any

import httpx

from .ratelimit import HostRateLimiter


USER_AGENT = "AIJobMatchBot/0.2 (+portfolio crawler; contact: local-dev)"
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0


class CrawlFetchError(Exception):
    def __init__(self, message: str, permanent: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.status = status


def backoff_seconds(attempt: int) -> float:
    base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt))
    return base * (1 + random.uniform(-0.2, 0.2))


def retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "")
    if header.isdigit():
        return min(float(header), 120.0)
    return backoff_seconds(attempt)


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    limiter: HostRateLimiter | None = None,
    max_attempts: int = 4,
) -> Any:
    host = urllib.parse.urlparse(url).netloc
    last_error = "unknown"

    for attempt in range(max_attempts):
        if limiter is not None:
            await limiter.acquire(host)
        final_attempt = attempt == max_attempts - 1

        try:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            last_error = f"network error: {exc}"
            if not final_attempt:
                await asyncio.sleep(backoff_seconds(attempt))
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise CrawlFetchError(f"{url} returned invalid JSON: {exc}", permanent=True, status=200) from exc

        if response.status_code == 429:
            last_error = "rate limited (429)"
            if not final_attempt:
                await asyncio.sleep(retry_after_seconds(response, attempt))
            continue

        if response.status_code >= 500:
            last_error = f"server error ({response.status_code})"
            if not final_attempt:
                await asyncio.sleep(backoff_seconds(attempt))
            continue

        raise CrawlFetchError(
            f"{url} returned HTTP {response.status_code}", permanent=True, status=response.status_code
        )

    raise CrawlFetchError(f"{url} failed after {max_attempts} attempts: {last_error}")
