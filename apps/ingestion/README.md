# Ingestion Pipeline

Real-time job ingestion for the main site. Four independent worker processes connected
by Redis Streams turn Greenhouse/Lever public boards into fresh rows in Postgres + pgvector:

```
scheduler ──► ingest:crawl.tasks ──► crawl-worker ──► ingest:jobs.raw ──► upserter ──► ingest:jobs.embed ──► embedder
   (due-source scan,                  (async httpx,                        (fingerprint diff,               (batch 64/request,
    idempotent enqueue)                token-bucket rate limit)             idempotent upsert)               pgvector write)
```

Design doc: [docs/INGESTION_PIPELINE_DESIGN.md](../../docs/INGESTION_PIPELINE_DESIGN.md)

## Run

Everything (workers included) is wired into docker compose at the repo root:

```bash
docker compose up -d --build
docker compose logs -f ingestion-scheduler ingestion-crawler ingestion-upserter ingestion-embedder
```

Scale the crawl workers:

```bash
docker compose up -d --scale ingestion-crawler=3
```

Locally without Docker (needs Postgres + Redis from compose):

```bash
cd apps/ingestion
PYTHONPATH=.:../api python -m ingestion.scheduler      # one process per terminal
PYTHONPATH=.:../api python -m ingestion.crawl_worker
PYTHONPATH=.:../api python -m ingestion.upserter
PYTHONPATH=.:../api python -m ingestion.embedder
```

Retire the hand-written seed postings once real jobs are flowing:

```bash
PYTHONPATH=.:../api python -m ingestion.retire_seed
```

## Operate

- `GET /ingestion/status` — stream depths, pending counts, DLQ sizes, per-source last success/error, job counts.
- `GET /ingestion/dlq` — inspect dead-lettered messages.
- `POST /ingestion/dlq/replay` — `{"stream": "ingest:jobs.raw"}` re-enqueues dead letters.

## Failure semantics

- Delivery is at-least-once (Redis Streams consumer groups); every consumer is idempotent,
  so replays and duplicate deliveries are no-ops.
- A crashed worker's pending messages are reclaimed by any peer after `CLAIM_IDLE_MS` (5 min —
  deliberately longer than the worst legitimate crawl: `Retry-After` up to 120s plus rate-limit waits).
- A message that fails `MAX_DELIVERIES` (5) times moves to `<stream>.dlq` with its error.
- Messages with an unknown future `schema_version` are quarantined to the DLQ, never crash a consumer.
- Per-host token bucket (Redis Lua) caps outbound crawl traffic; 429s honor `Retry-After`.
- Jobs a source stops returning are closed after 3 missed crawl cycles, never deleted.

## Test

```bash
cd apps/ingestion && pytest tests/ -q   # fakeredis, no external services
```
