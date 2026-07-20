import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from app.ingestion import greenhouse_job, lever_job, source_url
from app.models import CompanySource, JobPosting

from . import settings, stats, streams
from .db import SourceStore, ensure_pipeline_schema
from .envelope import EVENT_JOB_RAW, Envelope, EnvelopeError, decode, new_envelope
from .fetcher import CrawlFetchError, fetch_json
from .ratelimit import HostRateLimiter
from .streams import PermanentError, StreamConsumer, StreamMessage


logger = logging.getLogger("ingestion.crawl_worker")


def consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def parse_board(source: CompanySource, payload: Any) -> list[JobPosting]:
    if source.ats_type == "greenhouse":
        rows = payload.get("jobs", []) if isinstance(payload, dict) else []
        return [job for row in rows if (job := greenhouse_job(source, row))]
    if source.ats_type == "lever":
        rows = payload if isinstance(payload, list) else []
        return [job for row in rows if (job := lever_job(source, row))]
    raise PermanentError(f"Unsupported ats_type for crawl worker: {source.ats_type}")


async def crawl_task(
    envelope: Envelope,
    client: httpx.AsyncClient,
    limiter: HostRateLimiter,
    redis: Any,
) -> tuple[str, int]:
    source = CompanySource.model_validate(envelope.payload["source"])
    payload = await fetch_json(client, source_url(source), limiter)
    jobs = parse_board(source, payload)

    fetched_at = datetime.now(timezone.utc).isoformat()
    pipe = redis.pipeline(transaction=False)
    for job in jobs:
        streams.publish(
            redis,
            streams.JOBS_RAW,
            new_envelope(EVENT_JOB_RAW, {"job": job.model_dump(by_alias=True), "sourceId": source.id, "fetchedAt": fetched_at}),
            pipeline=pipe,
        )
    await asyncio.to_thread(pipe.execute)
    return source.id, len(jobs)


async def process_message(
    message: StreamMessage,
    consumer: StreamConsumer,
    client: httpx.AsyncClient,
    limiter: HostRateLimiter,
    redis: Any,
    source_store: SourceStore,
    semaphore: asyncio.Semaphore,
) -> None:
    # Sync psycopg/redis clients must not run on the event loop: a hung Postgres
    # connect would freeze every in-flight crawl. Everything blocking goes through
    # asyncio.to_thread.
    async with semaphore:
        try:
            envelope = decode(message.fields)
        except EnvelopeError as exc:
            await asyncio.to_thread(consumer.dead_letter, message, f"undecodable: {exc}")
            return

        raw_source = envelope.payload.get("source")
        source_id = str(raw_source.get("id", "unknown")) if isinstance(raw_source, dict) else "unknown"
        try:
            source_id, published = await crawl_task(envelope, client, limiter, redis)
        except (KeyError, TypeError, ValidationError) as exc:
            await asyncio.to_thread(consumer.dead_letter, message, f"invalid crawl task payload: {exc}")
            return
        except PermanentError as exc:
            await asyncio.to_thread(source_store.mark_error, source_id, str(exc))
            await asyncio.to_thread(consumer.dead_letter, message, str(exc))
            return
        except CrawlFetchError as exc:
            await asyncio.to_thread(source_store.mark_error, source_id, str(exc))
            if exc.permanent or consumer.exhausted(message):
                await asyncio.to_thread(consumer.dead_letter, message, str(exc))
            else:
                logger.warning("crawl of %s failed (delivery %d), leaving pending: %s", source_id, message.delivery_count, exc)
            return
        except Exception as exc:
            await asyncio.to_thread(source_store.mark_error, source_id, str(exc))
            if consumer.exhausted(message):
                await asyncio.to_thread(consumer.dead_letter, message, f"retries exhausted: {exc}")
            else:
                logger.exception("crawl of %s errored (delivery %d), leaving pending", source_id, message.delivery_count)
            return

        await asyncio.to_thread(source_store.mark_success, source_id)
        await asyncio.to_thread(finalize_success, consumer, message, redis, published)
        logger.info("crawled %s: published %d jobs", source_id, published)


def finalize_success(consumer: StreamConsumer, message: StreamMessage, redis: Any, published: int) -> None:
    stats.incr(redis, "sources_crawled")
    stats.incr(redis, "jobs_published", published)
    stats.mark_activity(redis, "crawl_worker")
    consumer.ack(message)


async def run(consumer: StreamConsumer, redis: Any, source_store: SourceStore) -> None:
    limiter = HostRateLimiter(redis, settings.RATE_LIMIT_PER_HOST, settings.RATE_LIMIT_BURST)
    semaphore = asyncio.Semaphore(settings.CRAWL_CONCURRENCY)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        while True:
            messages = await asyncio.to_thread(consumer.read)
            if not messages:
                continue
            outcomes = await asyncio.gather(
                *(process_message(message, consumer, client, limiter, redis, source_store, semaphore) for message in messages),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    logger.error("unhandled crawl error (message left pending): %s", outcome)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    redis = settings.get_redis()
    ensure_pipeline_schema()
    consumer = StreamConsumer(redis, streams.CRAWL_TASKS, "crawlers", consumer_name())
    asyncio.run(run(consumer, redis, SourceStore()))


if __name__ == "__main__":
    main()
