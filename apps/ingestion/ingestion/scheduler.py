import logging
import time
from typing import Any

from app.seed import load_company_sources

from . import settings, stats, streams
from .db import JobStore, SourceStore, ensure_pipeline_schema
from .envelope import EVENT_CRAWL_TASK, new_envelope


logger = logging.getLogger("ingestion.scheduler")


def dedup_key(source_id: str, interval_minutes: int, now_ts: float) -> str:
    window = int(now_ts // (max(1, interval_minutes) * 60))
    return f"ingest:dedup:crawl:{source_id}:{window}"


def enqueue_due(
    redis: Any,
    source_store: SourceStore,
    ats_types: tuple[str, ...] = settings.CRAWL_ATS_TYPES,
    now_ts: float | None = None,
) -> list[str]:
    now_ts = time.time() if now_ts is None else now_ts
    enqueued: list[str] = []

    for source in source_store.due_sources(ats_types):
        key = dedup_key(source.id, source.crawl_interval_minutes, now_ts)
        # SET NX makes enqueueing idempotent across scheduler restarts and replicas:
        # one crawl task per source per interval window, no matter who ticks.
        if not redis.set(key, "1", nx=True, ex=max(60, source.crawl_interval_minutes * 60)):
            continue

        envelope = new_envelope(
            EVENT_CRAWL_TASK,
            {"source": source.model_dump(by_alias=True), "window": key},
        )
        try:
            streams.publish(redis, streams.CRAWL_TASKS, envelope)
        except Exception:
            # Undo the window claim, or this source silently skips a whole interval.
            redis.delete(key)
            raise
        enqueued.append(source.id)

    if enqueued:
        source_store.mark_enqueued(enqueued)
        stats.incr(redis, "crawl_tasks_enqueued", len(enqueued))
        stats.mark_activity(redis, "scheduler")
    return enqueued


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    redis = settings.get_redis()
    ensure_pipeline_schema()

    source_store = SourceStore()
    job_store = JobStore()
    synced = source_store.sync_from_json(load_company_sources())
    logger.info("synced %d company sources into Postgres", synced)

    while True:
        try:
            enqueued = enqueue_due(redis, source_store)
            if enqueued:
                logger.info("enqueued crawl tasks: %s", ", ".join(enqueued))
            closed = job_store.close_stale_jobs(settings.STALE_CRAWL_CYCLES)
            if closed:
                stats.incr(redis, "jobs_closed", closed)
                logger.info("closed %d stale jobs", closed)
        except Exception:
            logger.exception("scheduler tick failed")
        time.sleep(settings.SCHEDULER_TICK_SECONDS)


if __name__ == "__main__":
    main()
