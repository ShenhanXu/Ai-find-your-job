"""Operational endpoints for the ingestion pipeline: queue status and DLQ inspection/replay."""

import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .database import get_connection


router = APIRouter(prefix="/ingestion", tags=["ingestion-pipeline"])

STREAM_GROUPS = {
    "ingest:crawl.tasks": "crawlers",
    "ingest:jobs.raw": "upserters",
    "ingest:jobs.embed": "embedders",
}
COUNTERS_KEY = "ingest:counters"


class DlqReplayRequest(BaseModel):
    stream: str
    ids: list[str] = Field(default_factory=list)
    limit: int = 100


def pipeline_redis() -> Any:
    import redis

    return redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )


def dlq_name(stream: str) -> str:
    return f"{stream}.dlq"


def queue_snapshot(client: Any) -> dict[str, Any]:
    import redis

    snapshot: dict[str, Any] = {}
    for stream, group in STREAM_GROUPS.items():
        pending = 0
        try:
            pending = int(client.xpending(stream, group)["pending"])
        except redis.ResponseError:
            pass  # group not created yet (workers never started)
        snapshot[stream] = {
            "length": client.xlen(stream),
            "pending": pending,
            "dlq": client.xlen(dlq_name(stream)),
        }
    return snapshot


def database_snapshot() -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(status, 'open') AS status, count(*), count(*) FILTER (WHERE embedding IS NOT NULL)
                FROM job_postings
                GROUP BY 1
                """
            )
            jobs = {
                row[0]: {"total": row[1], "embedded": row[2]}
                for row in cursor.fetchall()
            }
            cursor.execute(
                """
                SELECT id, company, enabled, last_enqueued_at, last_success_at, last_error, last_error_at
                FROM company_sources
                WHERE enabled
                ORDER BY last_success_at DESC NULLS LAST
                """
            )
            sources = [
                {
                    "id": row[0],
                    "company": row[1],
                    "enabled": row[2],
                    "last_enqueued_at": row[3].isoformat() if row[3] else None,
                    "last_success_at": row[4].isoformat() if row[4] else None,
                    "last_error": row[5],
                    "last_error_at": row[6].isoformat() if row[6] else None,
                }
                for row in cursor.fetchall()
            ]
    return {"jobs": jobs, "sources": sources}


@router.get("/status")
def ingestion_status() -> dict[str, Any]:
    status: dict[str, Any] = {"queue": "unavailable", "database": "unavailable"}

    try:
        client = pipeline_redis()
        status["streams"] = queue_snapshot(client)
        status["counters"] = client.hgetall(COUNTERS_KEY) or {}
        status["queue"] = "connected"
    except Exception as exc:
        status["queue_error"] = str(exc)

    try:
        status.update(database_snapshot())
        status["database"] = "connected"
    except Exception as exc:
        status["database_error"] = str(exc)

    return status


@router.get("/dlq")
def list_dlq(limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    try:
        client = pipeline_redis()
        result: dict[str, list[dict[str, Any]]] = {}
        for stream in STREAM_GROUPS:
            entries = []
            for message_id, fields in client.xrevrange(dlq_name(stream), count=max(1, min(limit, 100))):
                event_type = ""
                try:
                    event_type = json.loads(fields.get("data", "{}")).get("type", "")
                except (json.JSONDecodeError, AttributeError):
                    pass
                entries.append(
                    {
                        "id": message_id,
                        "type": event_type,
                        "error": fields.get("error", ""),
                        "deliveries": fields.get("deliveries", ""),
                        "original_id": fields.get("original_id", ""),
                    }
                )
            result[stream] = entries
        return result
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Queue unavailable: {exc}") from exc


@router.post("/dlq/replay")
def replay_dlq(payload: DlqReplayRequest) -> dict[str, int]:
    if payload.stream not in STREAM_GROUPS:
        raise HTTPException(status_code=400, detail=f"Unknown stream. Expected one of: {', '.join(STREAM_GROUPS)}")

    try:
        client = pipeline_redis()
        source = dlq_name(payload.stream)
        if payload.ids:
            entries = [(message_id, client.xrange(source, min=message_id, max=message_id)) for message_id in payload.ids]
            entries = [(mid, rows[0][1]) for mid, rows in entries if rows]
        else:
            entries = client.xrange(source, count=max(1, min(payload.limit, 500)))

        replayed = 0
        for message_id, fields in entries:
            data = fields.get("data")
            if not data:
                continue
            client.xadd(payload.stream, {"data": data})
            client.xdel(source, message_id)
            replayed += 1
        return {"replayed": replayed}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Queue unavailable: {exc}") from exc
