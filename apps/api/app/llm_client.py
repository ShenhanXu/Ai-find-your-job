"""Shared HTTP layer for LLM and embedding API calls.

Replaces ad-hoc urllib usage with pooled httpx clients, bounded retries with
exponential backoff (honoring Retry-After), and token-usage recording into the
workflow trace. All failures surface as LLMRequestError, an OSError subclass,
so existing `except (OSError, ...)` fallbacks keep working.
"""

import asyncio
import logging
import os
import random
import time
from typing import Any, AsyncIterator

import httpx

from .workflow_trace import record_trace_event


logger = logging.getLogger("app.llm")

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = max(1, int(os.getenv("LLM_HTTP_MAX_ATTEMPTS", "3")))
BACKOFF_BASE_SECONDS = float(os.getenv("LLM_HTTP_BACKOFF_SECONDS", "0.5"))
RETRY_AFTER_CAP_SECONDS = 30.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class LLMRequestError(OSError):
    """Raised when an upstream AI API call fails after retries.

    Subclasses OSError so the existing chat/router fallback handlers catch it.
    Messages never include request headers, so API keys cannot leak.
    """


_sync_client: httpx.Client | None = None
_async_client: httpx.AsyncClient | None = None


def get_sync_client() -> httpx.Client:
    global _sync_client
    if _sync_client is None or _sync_client.is_closed:
        _sync_client = httpx.Client(
            timeout=DEFAULT_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _sync_client


def get_async_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        _async_client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _async_client


async def aclose_clients() -> None:
    global _sync_client, _async_client
    if _sync_client is not None and not _sync_client.is_closed:
        _sync_client.close()
    _sync_client = None
    if _async_client is not None and not _async_client.is_closed:
        await _async_client.aclose()
    _async_client = None


def record_usage(service: str, model: str, usage: dict[str, Any]) -> None:
    """Record token usage from an API response into the trace and logs."""
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    total_tokens = usage.get("total_tokens")
    record_trace_event(
        "llm_usage",
        {
            "service": service,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    )
    logger.info(
        "llm_usage service=%s model=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
        service,
        model,
        prompt_tokens,
        completion_tokens,
        total_tokens,
    )


def _record_payload_usage(service: str, model: str, payload: Any) -> None:
    if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
        record_usage(service, model, payload["usage"])


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(RETRY_AFTER_CAP_SECONDS, float(retry_after))
            except ValueError:
                pass
    return BACKOFF_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.25)


def _status_error(service: str, response: httpx.Response) -> LLMRequestError:
    return LLMRequestError(
        f"{service} request failed with HTTP {response.status_code}: {response.text[:300]}"
    )


def post_json(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    service: str,
    model: str = "",
) -> dict[str, Any]:
    """POST JSON with pooled connections and bounded retry (sync callers)."""
    last_error: str | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = get_sync_client().post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            time.sleep(_retry_delay(attempt, None))
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and attempt + 1 < MAX_ATTEMPTS:
            last_error = f"HTTP {response.status_code}"
            time.sleep(_retry_delay(attempt, response))
            continue
        if response.status_code >= 400:
            raise _status_error(service, response)

        payload = response.json()
        _record_payload_usage(service, model, payload)
        return payload

    raise LLMRequestError(f"{service} request failed after {MAX_ATTEMPTS} attempts: {last_error}")


async def post_json_async(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    service: str,
    model: str = "",
) -> dict[str, Any]:
    """POST JSON with pooled connections and bounded retry (async callers)."""
    last_error: str | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = await get_async_client().post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            await asyncio.sleep(_retry_delay(attempt, None))
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and attempt + 1 < MAX_ATTEMPTS:
            last_error = f"HTTP {response.status_code}"
            await asyncio.sleep(_retry_delay(attempt, response))
            continue
        if response.status_code >= 400:
            raise _status_error(service, response)

        payload = response.json()
        _record_payload_usage(service, model, payload)
        return payload

    raise LLMRequestError(f"{service} request failed after {MAX_ATTEMPTS} attempts: {last_error}")


async def stream_sse_lines(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    service: str,
) -> AsyncIterator[str]:
    """Stream response lines over SSE. Retries only before the first line is
    yielded; a mid-stream failure is surfaced instead of replaying content."""
    last_error: str | None = None
    for attempt in range(MAX_ATTEMPTS):
        yielded = False
        should_retry = False
        delay = 0.0
        try:
            async with get_async_client().stream(
                "POST", url, json=body, headers=headers, timeout=timeout
            ) as response:
                if response.status_code in RETRYABLE_STATUS_CODES and attempt + 1 < MAX_ATTEMPTS:
                    await response.aread()
                    last_error = f"HTTP {response.status_code}"
                    should_retry = True
                    delay = _retry_delay(attempt, response)
                elif response.status_code >= 400:
                    detail = (await response.aread()).decode("utf-8", "replace")
                    raise LLMRequestError(
                        f"{service} stream failed with HTTP {response.status_code}: {detail[:300]}"
                    )
                else:
                    async for line in response.aiter_lines():
                        yielded = True
                        yield line
                    return
        except httpx.HTTPError as exc:
            if yielded:
                raise LLMRequestError(
                    f"{service} stream failed mid-response ({type(exc).__name__})."
                ) from exc
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 >= MAX_ATTEMPTS:
                break
            await asyncio.sleep(_retry_delay(attempt, None))
            continue

        if should_retry:
            await asyncio.sleep(delay)

    raise LLMRequestError(f"{service} stream failed after {MAX_ATTEMPTS} attempts: {last_error}")
