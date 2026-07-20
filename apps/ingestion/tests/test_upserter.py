from ingestion import streams
from ingestion.envelope import EVENT_JOB_RAW, decode, new_envelope
from ingestion.streams import StreamConsumer, dlq_name, handle_message
from ingestion.upserter import build_handler

from .conftest import make_job


class FakeJobStore:
    """In-memory stand-in mirroring JobStore.upsert's (outcome, needs_embedding) contract."""

    def __init__(self):
        self.rows: dict[str, str] = {}
        self.embedded: set[str] = set()

    def upsert(self, job, source_id):
        previous = self.rows.get(job.id)
        self.rows[job.id] = job.fingerprint
        if previous is None:
            outcome = "added"
        elif previous != job.fingerprint:
            outcome = "updated"
            self.embedded.discard(job.id)  # fingerprint change nulls the vector
        else:
            outcome = "unchanged"
        return outcome, outcome != "unchanged" or job.id not in self.embedded

    def mark_embedded(self, job_id):
        self.embedded.add(job_id)


def make_consumer(redis_client) -> StreamConsumer:
    return StreamConsumer(redis_client, streams.JOBS_RAW, "upserters", "test-upserter", batch=10, block_ms=1, claim_idle_ms=0)


def publish_job(redis_client, job):
    streams.publish(
        redis_client,
        streams.JOBS_RAW,
        new_envelope(EVENT_JOB_RAW, {"job": job.model_dump(by_alias=True), "sourceId": "acme"}),
    )


def consume_all(redis_client, handler):
    consumer = make_consumer(redis_client)
    for message in consumer.read():
        handle_message(consumer, message, handler)


def embed_events(redis_client):
    return [decode(fields).payload for _, fields in redis_client.xrange(streams.JOBS_EMBED)]


def simulate_embed_success(redis_client, store, job):
    """What the embedder does after a successful embed: store the vector, release the dedup key."""
    store.mark_embedded(job.id)
    redis_client.delete(f"ingest:dedup:embed:{job.id}:{job.fingerprint}")


def test_new_job_is_added_and_embed_task_emitted(redis_client):
    store = FakeJobStore()
    handler = build_handler(redis_client, store)
    publish_job(redis_client, make_job())

    consume_all(redis_client, handler)

    assert store.rows == {"acme-1": "fp-1"}
    assert embed_events(redis_client) == [{"jobId": "acme-1", "fingerprint": "fp-1"}]


def test_replay_after_embed_is_fully_idempotent(redis_client):
    store = FakeJobStore()
    handler = build_handler(redis_client, store)
    publish_job(redis_client, make_job())
    consume_all(redis_client, handler)
    simulate_embed_success(redis_client, store, make_job())

    # Same message delivered again (at-least-once): no second embed task.
    publish_job(redis_client, make_job())
    consume_all(redis_client, handler)

    assert redis_client.xlen(streams.JOBS_EMBED) == 1
    assert redis_client.hget("ingest:counters", "jobs_unchanged") == "1"


def test_unchanged_with_missing_embedding_self_heals(redis_client):
    """Crash-window recovery: the embed task vanished but the row has no vector —
    an 'unchanged' redelivery must re-emit the task instead of dropping it forever."""
    store = FakeJobStore()
    handler = build_handler(redis_client, store)
    publish_job(redis_client, make_job())
    consume_all(redis_client, handler)

    # Simulate the lost task: job was never embedded. Replay the same raw event.
    publish_job(redis_client, make_job())
    consume_all(redis_client, handler)

    assert redis_client.xlen(streams.JOBS_EMBED) == 2  # re-emitted

    # A burst of duplicates within the retry-gate TTL adds nothing more.
    publish_job(redis_client, make_job())
    consume_all(redis_client, handler)
    assert redis_client.xlen(streams.JOBS_EMBED) == 2


def test_fingerprint_flip_flop_still_gets_embedded(redis_client):
    """A→B→A within the dedup TTL: the embedder released A's key after embedding,
    so the third upsert can re-emit an embed task for A."""
    store = FakeJobStore()
    handler = build_handler(redis_client, store)

    job_a = make_job(fingerprint="fp-a")
    job_b = make_job(fingerprint="fp-b")

    publish_job(redis_client, job_a)
    consume_all(redis_client, handler)
    simulate_embed_success(redis_client, store, job_a)

    publish_job(redis_client, job_b)
    consume_all(redis_client, handler)
    simulate_embed_success(redis_client, store, job_b)

    publish_job(redis_client, job_a)
    consume_all(redis_client, handler)

    fingerprints = [event["fingerprint"] for event in embed_events(redis_client)]
    assert fingerprints == ["fp-a", "fp-b", "fp-a"]


def test_changed_fingerprint_emits_new_embed_task(redis_client):
    store = FakeJobStore()
    handler = build_handler(redis_client, store)
    publish_job(redis_client, make_job(fingerprint="fp-1"))
    consume_all(redis_client, handler)
    publish_job(redis_client, make_job(fingerprint="fp-2"))
    consume_all(redis_client, handler)

    fingerprints = [event["fingerprint"] for event in embed_events(redis_client)]
    assert fingerprints == ["fp-1", "fp-2"]
    assert redis_client.hget("ingest:counters", "jobs_updated") == "1"


def test_invalid_job_payload_goes_to_dlq(redis_client):
    handler = build_handler(redis_client, FakeJobStore())
    streams.publish(redis_client, streams.JOBS_RAW, new_envelope(EVENT_JOB_RAW, {"job": {"id": "only-an-id"}}))

    consume_all(redis_client, handler)

    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 1
    assert redis_client.xpending(streams.JOBS_RAW, "upserters")["pending"] == 0


def test_unexpected_event_type_goes_to_dlq(redis_client):
    handler = build_handler(redis_client, FakeJobStore())
    streams.publish(redis_client, streams.JOBS_RAW, new_envelope("crawl.task", {"source": {}}))

    consume_all(redis_client, handler)

    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 1
