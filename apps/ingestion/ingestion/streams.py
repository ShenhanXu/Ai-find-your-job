import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import settings
from .envelope import Envelope, EnvelopeError, decode, encode


logger = logging.getLogger("ingestion.streams")

CRAWL_TASKS = "ingest:crawl.tasks"
JOBS_RAW = "ingest:jobs.raw"
JOBS_EMBED = "ingest:jobs.embed"
WORK_STREAMS = (CRAWL_TASKS, JOBS_RAW, JOBS_EMBED)


class PermanentError(Exception):
    """Raised by handlers for messages that will never succeed; sends them straight to the DLQ."""


def dlq_name(stream: str) -> str:
    return f"{stream}.dlq"


def publish(redis: Any, stream: str, envelope: Envelope, pipeline: Any = None) -> None:
    target = pipeline if pipeline is not None else redis
    target.xadd(stream, encode(envelope), maxlen=settings.STREAM_MAXLEN, approximate=True)


@dataclass
class StreamMessage:
    message_id: str
    fields: dict[Any, Any]
    delivery_count: int = 1


class StreamConsumer:
    def __init__(
        self,
        redis: Any,
        stream: str,
        group: str,
        consumer: str,
        batch: int = settings.CONSUMER_BATCH,
        block_ms: int = settings.CONSUMER_BLOCK_MS,
        claim_idle_ms: int = settings.CLAIM_IDLE_MS,
        max_deliveries: int = settings.MAX_DELIVERIES,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.batch = batch
        self.block_ms = block_ms
        self.claim_idle_ms = claim_idle_ms
        self.max_deliveries = max_deliveries
        self.ensure_group()

    def ensure_group(self) -> None:
        import redis as redis_lib

        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis_lib.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def read(self, count: int | None = None) -> list[StreamMessage]:
        count = count or self.batch
        messages = self._claim_stale(count)
        if len(messages) >= count:
            return messages[:count]

        # block=None (not 0) when we already have claimed work: in Redis, BLOCK 0 waits forever.
        response = self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=count - len(messages),
            block=self.block_ms if not messages else None,
        )
        for _, entries in response or []:
            for message_id, fields in entries:
                messages.append(StreamMessage(message_id=str(message_id), fields=fields, delivery_count=1))
        return messages

    def _claim_stale(self, count: int) -> list[StreamMessage]:
        """Take over messages another consumer read but never acked (crashed worker)."""
        response = self.redis.xautoclaim(
            self.stream, self.group, self.consumer, min_idle_time=self.claim_idle_ms, start_id="0", count=count
        )
        entries = response[1] if isinstance(response, (list, tuple)) and len(response) >= 2 else []
        if not entries:
            return []

        # Query the PEL over exactly the claimed id range — a bounded scan from "-"
        # could miss our ids in a large backlog and undercount deliveries, so
        # poison messages would never reach the DLQ threshold.
        claimed_ids = [str(message_id) for message_id, fields in entries if fields is not None]
        deliveries: dict[str, int] = {}
        if claimed_ids:
            for pending in self.redis.xpending_range(
                self.stream, self.group, min=claimed_ids[0], max=claimed_ids[-1], count=len(entries) * 2
            ):
                deliveries[str(pending["message_id"])] = int(pending.get("times_delivered") or 1)

        messages = []
        for message_id, fields in entries:
            if fields is None:
                continue
            message_id = str(message_id)
            messages.append(
                StreamMessage(message_id=message_id, fields=fields, delivery_count=deliveries.get(message_id, 1))
            )
        return messages

    def ack(self, message: StreamMessage) -> None:
        self.redis.xack(self.stream, self.group, message.message_id)

    def dead_letter(self, message: StreamMessage, error: str) -> None:
        raw = message.fields.get("data") or message.fields.get(b"data") or ""
        # XADD + XACK in one MULTI: a crash between them would otherwise leave the
        # message pending with a DLQ copy already written (double dead-letter).
        pipe = self.redis.pipeline(transaction=True)
        pipe.xadd(
            dlq_name(self.stream),
            {
                "data": raw,
                "error": str(error)[:500],
                "source_stream": self.stream,
                "original_id": message.message_id,
                "deliveries": str(message.delivery_count),
            },
            maxlen=10_000,
            approximate=True,
        )
        pipe.xack(self.stream, self.group, message.message_id)
        pipe.execute()
        logger.warning("dead-lettered %s from %s: %s", message.message_id, self.stream, str(error)[:200])

    def exhausted(self, message: StreamMessage) -> bool:
        return message.delivery_count >= self.max_deliveries


def handle_message(consumer: StreamConsumer, message: StreamMessage, handler: Callable[[Envelope], None]) -> bool:
    """Process one message with the shared retry/DLQ policy. Returns True when acked."""
    try:
        envelope = decode(message.fields)
    except EnvelopeError as exc:
        consumer.dead_letter(message, f"undecodable: {exc}")
        return False

    try:
        handler(envelope)
    except PermanentError as exc:
        consumer.dead_letter(message, str(exc))
        return False
    except Exception as exc:
        if consumer.exhausted(message):
            consumer.dead_letter(message, f"retries exhausted: {exc}")
        else:
            logger.warning(
                "message %s failed (delivery %d/%d), leaving pending: %s",
                message.message_id,
                message.delivery_count,
                consumer.max_deliveries,
                exc,
            )
        return False

    consumer.ack(message)
    return True


def run_consumer(
    consumer: StreamConsumer,
    handler: Callable[[Envelope], None],
    max_batches: int | None = None,
    idle_sleep_seconds: float = 0.0,
) -> None:
    batches = 0
    while max_batches is None or batches < max_batches:
        batches += 1
        messages = consumer.read()
        if not messages:
            if idle_sleep_seconds:
                time.sleep(idle_sleep_seconds)
            continue
        for message in messages:
            handle_message(consumer, message, handler)
