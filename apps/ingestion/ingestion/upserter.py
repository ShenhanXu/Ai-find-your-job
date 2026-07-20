import logging
import os
import socket
from typing import Any, Callable

from pydantic import ValidationError

from app.database import job_fingerprint
from app.models import JobPosting

from . import settings, stats, streams
from .db import JobStore, UPSERT_OUTCOME_UNCHANGED, ensure_pipeline_schema
from .envelope import EVENT_JOB_EMBED, EVENT_JOB_RAW, Envelope, new_envelope
from .streams import PermanentError, StreamConsumer, run_consumer


logger = logging.getLogger("ingestion.upserter")


def build_handler(redis: Any, store: JobStore) -> Callable[[Envelope], None]:
    def handler(envelope: Envelope) -> None:
        if envelope.type != EVENT_JOB_RAW:
            raise PermanentError(f"unexpected event type on jobs.raw: {envelope.type}")

        try:
            job = JobPosting.model_validate(envelope.payload["job"])
        except (KeyError, ValidationError) as exc:
            raise PermanentError(f"invalid job payload: {exc}") from exc

        if not job.fingerprint:
            job.fingerprint = job_fingerprint(job)

        source_id = envelope.payload.get("sourceId")
        outcome, needs_embedding = store.upsert(job, str(source_id) if source_id else None)
        stats.incr(redis, f"jobs_{outcome}")
        stats.mark_activity(redis, "upserter")

        if not needs_embedding:
            return

        embed_event = new_envelope(EVENT_JOB_EMBED, {"jobId": job.id, "fingerprint": job.fingerprint})

        if outcome == UPSERT_OUTCOME_UNCHANGED:
            # The row is missing its vector even though nothing changed: an earlier
            # consumer crashed between DB commit and publish, or a fingerprint
            # flip-flop swallowed the task. Re-emit behind a short-TTL gate so a
            # burst of duplicate raw events yields one retry task, not many.
            if redis.set(f"ingest:dedup:embed-retry:{job.id}:{job.fingerprint}", "1", nx=True, ex=900):
                streams.publish(redis, streams.JOBS_EMBED, embed_event)
            return

        # Dedup embed tasks per (job, fingerprint); the embedder releases this key
        # after a successful embed and also guards on fingerprint, so duplicates
        # that slip through are still no-ops.
        if redis.set(f"ingest:dedup:embed:{job.id}:{job.fingerprint}", "1", nx=True, ex=settings.EMBED_DEDUP_TTL_SECONDS):
            streams.publish(redis, streams.JOBS_EMBED, embed_event)

    return handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    redis = settings.get_redis()
    ensure_pipeline_schema()
    consumer = StreamConsumer(redis, streams.JOBS_RAW, "upserters", f"{socket.gethostname()}-{os.getpid()}")
    logger.info("upserter consuming %s", streams.JOBS_RAW)
    run_consumer(consumer, build_handler(redis, JobStore()))


if __name__ == "__main__":
    main()
