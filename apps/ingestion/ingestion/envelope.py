import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1

EVENT_CRAWL_TASK = "crawl.task"
EVENT_JOB_RAW = "job.raw"
EVENT_JOB_EMBED = "job.embed"


class EnvelopeError(ValueError):
    pass


class SchemaVersionError(EnvelopeError):
    def __init__(self, version: int) -> None:
        super().__init__(f"Unsupported schema_version {version} (this consumer understands <= {SCHEMA_VERSION})")
        self.version = version


@dataclass(frozen=True)
class Envelope:
    type: str
    payload: dict[str, Any]
    schema_version: int = SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def new_envelope(event_type: str, payload: dict[str, Any]) -> Envelope:
    return Envelope(type=event_type, payload=payload)


def encode(envelope: Envelope) -> dict[str, str]:
    return {
        "data": json.dumps(
            {
                "schema_version": envelope.schema_version,
                "event_id": envelope.event_id,
                "occurred_at": envelope.occurred_at,
                "type": envelope.type,
                "payload": envelope.payload,
            }
        )
    }


def decode(fields: dict[Any, Any]) -> Envelope:
    raw = fields.get("data") or fields.get(b"data")
    if raw is None:
        raise EnvelopeError("Message has no 'data' field")

    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise EnvelopeError(f"Message data is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise EnvelopeError("Message data must be a JSON object")

    version = int(document.get("schema_version", 1))
    # Additive changes keep the same version and unknown keys are ignored;
    # only a version we have never seen is refused (and lands in the DLQ).
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(version)

    event_type = document.get("type")
    if not event_type:
        raise EnvelopeError("Message envelope is missing 'type'")

    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise EnvelopeError("Message envelope is missing 'payload' object")

    return Envelope(
        type=str(event_type),
        payload=payload,
        schema_version=version,
        event_id=str(document.get("event_id", "")),
        occurred_at=str(document.get("occurred_at", "")),
    )
