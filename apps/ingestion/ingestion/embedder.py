import logging
import os
import socket
import time
from typing import Any

from app.ai_chat import embed_document, get_embedding_provider, vector_literal
from app.database import job_search_text
from app.models import JobPosting

from . import settings, stats, streams
from .db import JobStore, ensure_pipeline_schema
from .envelope import EVENT_JOB_EMBED, EnvelopeError, decode
from .streams import StreamConsumer, StreamMessage


logger = logging.getLogger("ingestion.embedder")


def is_rate_limited(exc: Exception) -> bool:
    return "429" in str(exc)


def embed_jobs(provider: Any, jobs: list[JobPosting]) -> tuple[list[tuple[JobPosting, list[float]]], set[str]]:
    """Embed with one batch request when possible; fall back to per-item so a single
    poison text fails alone instead of dead-lettering the whole batch.
    Returns (embedded pairs, failed job ids)."""
    texts = [job_search_text(job) for job in jobs]

    batch = getattr(provider, "embed_batch", None)
    if callable(batch):
        try:
            vectors = batch(texts)
            if len(vectors) == len(jobs):
                return list(zip(jobs, vectors)), set()
            logger.warning("provider returned %d vectors for %d texts; retrying per item", len(vectors), len(jobs))
        except Exception as exc:
            if is_rate_limited(exc):
                # Rate limit hits the whole batch equally: per-item retries would only
                # hammer the provider harder. Let the queue redeliver later.
                logger.warning("embedding batch of %d rate-limited: %s", len(jobs), exc)
                return [], {job.id for job in jobs}
            logger.warning("embedding batch of %d failed (%s); retrying per item", len(jobs), exc)

    results: list[tuple[JobPosting, list[float]]] = []
    failed: set[str] = set()
    for job, text in zip(jobs, texts):
        try:
            results.append((job, embed_document(provider, text, job.title)))
        except Exception as exc:
            failed.add(job.id)
            logger.warning("embedding %s failed: %s", job.id, exc)
    return results, failed


def process_batch(
    messages: list[StreamMessage],
    consumer: StreamConsumer,
    redis: Any,
    store: JobStore,
    provider: Any,
) -> int:
    tasks: list[tuple[StreamMessage, str, str | None]] = []
    for message in messages:
        try:
            envelope = decode(message.fields)
            if envelope.type != EVENT_JOB_EMBED:
                raise EnvelopeError(f"unexpected event type on jobs.embed: {envelope.type}")
            job_id = str(envelope.payload["jobId"])
        except (EnvelopeError, KeyError) as exc:
            consumer.dead_letter(message, f"undecodable: {exc}")
            continue
        tasks.append((message, job_id, envelope.payload.get("fingerprint")))

    if not tasks:
        return 0

    fingerprints_by_id: dict[str, set[str | None]] = {}
    for _, job_id, fingerprint in tasks:
        fingerprints_by_id.setdefault(job_id, set()).add(fingerprint)

    pending_jobs = store.fetch_for_embedding(sorted(fingerprints_by_id))
    to_embed = [job for job in pending_jobs if job.fingerprint in fingerprints_by_id.get(job.id, set())]
    work_ids = {job.id for job in to_embed}

    # Tasks needing no work — already embedded, or superseded by a newer fingerprint
    # whose own task exists — are acked immediately so a provider outage cannot hold
    # them hostage in the pending list.
    work_tasks: list[tuple[StreamMessage, str, str | None]] = []
    for message, job_id, fingerprint in tasks:
        if job_id in work_ids:
            work_tasks.append((message, job_id, fingerprint))
        else:
            consumer.ack(message)

    embedded = 0
    if to_embed:
        results, failed_ids = embed_jobs(provider, to_embed)
        for job, vector in results:
            if store.set_embedding(job.id, job.fingerprint, vector_literal(vector)):
                embedded += 1
            # Release the dedup key so a later fingerprint flip-flop back to this
            # value can enqueue a fresh task.
            redis.delete(f"ingest:dedup:embed:{job.id}:{job.fingerprint}")

        for message, job_id, _ in work_tasks:
            if job_id not in failed_ids:
                consumer.ack(message)
            elif consumer.exhausted(message):
                consumer.dead_letter(message, "embedding failed repeatedly")
            # else: leave pending; the queue redelivers after the idle timeout.

    if embedded:
        stats.incr(redis, "jobs_embedded", embedded)
        stats.mark_activity(redis, "embedder")
    return embedded


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    redis = settings.get_redis()
    ensure_pipeline_schema()
    provider = get_embedding_provider()
    store = JobStore()
    consumer = StreamConsumer(
        redis, streams.JOBS_EMBED, "embedders", f"{socket.gethostname()}-{os.getpid()}", batch=settings.EMBED_BATCH_SIZE
    )
    logger.info("embedder consuming %s with provider %s", streams.JOBS_EMBED, provider.source)

    while True:
        messages = consumer.read(count=settings.EMBED_BATCH_SIZE)
        if not messages:
            continue
        embedded = process_batch(messages, consumer, redis, store, provider)
        if embedded:
            logger.info("embedded %d jobs", embedded)
        if settings.EMBED_PAUSE_SECONDS:
            time.sleep(settings.EMBED_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
