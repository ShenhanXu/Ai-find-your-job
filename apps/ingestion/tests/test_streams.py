import pytest

from ingestion import streams
from ingestion.envelope import new_envelope
from ingestion.streams import PermanentError, StreamConsumer, dlq_name, handle_message


def make_consumer(redis_client, **overrides) -> StreamConsumer:
    options = {"batch": 10, "block_ms": 1, "claim_idle_ms": 0, "max_deliveries": 3}
    options.update(overrides)
    return StreamConsumer(redis_client, streams.JOBS_RAW, "upserters", "test-consumer", **options)


def publish_one(redis_client, payload=None):
    streams.publish(redis_client, streams.JOBS_RAW, new_envelope("job.raw", payload or {"job": {"id": "x"}}))


def test_publish_read_ack(redis_client):
    consumer = make_consumer(redis_client)
    publish_one(redis_client)

    messages = consumer.read()
    assert len(messages) == 1
    consumer.ack(messages[0])

    assert consumer.read() == []
    assert redis_client.xpending(streams.JOBS_RAW, "upserters")["pending"] == 0


def test_unacked_message_is_reclaimed_by_another_consumer(redis_client):
    first = make_consumer(redis_client, claim_idle_ms=10_000)
    publish_one(redis_client)
    assert len(first.read()) == 1  # read but never acked (simulated crash)

    second = StreamConsumer(
        redis_client, streams.JOBS_RAW, "upserters", "other-consumer", batch=10, block_ms=1, claim_idle_ms=0
    )
    messages = second.read()
    assert len(messages) == 1
    # fakeredis omits times_delivered from XPENDING; real Redis reports >= 2 here.
    assert messages[0].delivery_count >= 1


def test_dead_letter_moves_message_and_acks(redis_client):
    consumer = make_consumer(redis_client)
    publish_one(redis_client)
    message = consumer.read()[0]

    consumer.dead_letter(message, "boom")

    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 1
    assert redis_client.xpending(streams.JOBS_RAW, "upserters")["pending"] == 0
    entry = redis_client.xrange(dlq_name(streams.JOBS_RAW))[0][1]
    assert entry["error"] == "boom"
    assert entry["source_stream"] == streams.JOBS_RAW


def test_handler_success_acks(redis_client):
    consumer = make_consumer(redis_client)
    publish_one(redis_client)
    message = consumer.read()[0]

    assert handle_message(consumer, message, lambda envelope: None) is True
    assert redis_client.xpending(streams.JOBS_RAW, "upserters")["pending"] == 0


def test_permanent_error_goes_to_dlq(redis_client):
    consumer = make_consumer(redis_client)
    publish_one(redis_client)
    message = consumer.read()[0]

    def handler(envelope):
        raise PermanentError("cannot ever succeed")

    assert handle_message(consumer, message, handler) is False
    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 1


def test_transient_error_leaves_message_pending(redis_client):
    consumer = make_consumer(redis_client)
    publish_one(redis_client)
    message = consumer.read()[0]

    def handler(envelope):
        raise RuntimeError("transient")

    assert handle_message(consumer, message, handler) is False
    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 0
    assert redis_client.xpending(streams.JOBS_RAW, "upserters")["pending"] == 1


def test_exhausted_deliveries_go_to_dlq(redis_client):
    consumer = make_consumer(redis_client)
    publish_one(redis_client)
    message = consumer.read()[0]
    message.delivery_count = consumer.max_deliveries

    def handler(envelope):
        raise RuntimeError("still failing")

    assert handle_message(consumer, message, handler) is False
    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 1
    assert redis_client.xpending(streams.JOBS_RAW, "upserters")["pending"] == 0


def test_undecodable_message_goes_to_dlq(redis_client):
    consumer = make_consumer(redis_client)
    redis_client.xadd(streams.JOBS_RAW, {"data": "not json"})
    message = consumer.read()[0]

    assert handle_message(consumer, message, lambda envelope: None) is False
    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 1


def test_replayed_dlq_message_is_consumable_again(redis_client):
    consumer = make_consumer(redis_client)
    publish_one(redis_client)
    message = consumer.read()[0]
    consumer.dead_letter(message, "boom")

    # Replay the way the admin endpoint does: XADD data back, XDEL from DLQ.
    dlq_entries = redis_client.xrange(dlq_name(streams.JOBS_RAW))
    for message_id, fields in dlq_entries:
        redis_client.xadd(streams.JOBS_RAW, {"data": fields["data"]})
        redis_client.xdel(dlq_name(streams.JOBS_RAW), message_id)

    assert redis_client.xlen(dlq_name(streams.JOBS_RAW)) == 0
    replayed = consumer.read()
    assert len(replayed) == 1
    assert handle_message(consumer, replayed[0], lambda envelope: None) is True
