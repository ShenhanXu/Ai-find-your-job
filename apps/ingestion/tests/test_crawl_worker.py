import asyncio
import json

import httpx

from ingestion import streams
from ingestion.crawl_worker import parse_board, process_message
from ingestion.envelope import EVENT_CRAWL_TASK, decode, new_envelope
from ingestion.streams import StreamConsumer, dlq_name

from .conftest import make_source


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 101,
            "title": "Software Engineer, Backend",
            "location": {"name": "Seattle, WA"},
            "content": "<p>Build <b>Python</b> services on AWS.</p>",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
        },
        {
            "id": 102,
            "title": "Recruiter",
            "location": {"name": "New York, NY"},
            "content": "<p>Hire people.</p>",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
        },
    ]
}


class FakeSourceStore:
    def __init__(self):
        self.successes: list[str] = []
        self.errors: list[tuple[str, str]] = []

    def mark_success(self, source_id):
        self.successes.append(source_id)

    def mark_error(self, source_id, message):
        self.errors.append((source_id, message))


def make_consumer(redis_client) -> StreamConsumer:
    return StreamConsumer(redis_client, streams.CRAWL_TASKS, "crawlers", "test-crawler", batch=10, block_ms=1, claim_idle_ms=0)


def enqueue_task(redis_client, source):
    streams.publish(
        redis_client,
        streams.CRAWL_TASKS,
        new_envelope(EVENT_CRAWL_TASK, {"source": source.model_dump(by_alias=True)}),
    )


def run_one(redis_client, transport, source_store):
    consumer = make_consumer(redis_client)
    message = consumer.read()[0]

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            await process_message(
                message, consumer, client, None, redis_client, source_store, asyncio.Semaphore(2)
            )

    asyncio.run(go())
    return consumer


def test_greenhouse_crawl_publishes_jobs(redis_client):
    source = make_source(id="acme")
    enqueue_task(redis_client, source)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=GREENHOUSE_PAYLOAD))
    source_store = FakeSourceStore()

    run_one(redis_client, transport, source_store)

    assert source_store.successes == ["acme"]
    assert redis_client.xpending(streams.CRAWL_TASKS, "crawlers")["pending"] == 0
    raw = redis_client.xrange(streams.JOBS_RAW)
    assert len(raw) == 2
    envelope = decode(raw[0][1])
    assert envelope.type == "job.raw"
    assert envelope.payload["sourceId"] == "acme"
    assert envelope.payload["job"]["company"] == "Acme"
    assert envelope.payload["job"]["fingerprint"]


def test_role_keywords_filter_jobs(redis_client):
    source = make_source(id="acme", roleKeywords=["software"])
    enqueue_task(redis_client, source)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=GREENHOUSE_PAYLOAD))

    run_one(redis_client, transport, FakeSourceStore())

    raw = redis_client.xrange(streams.JOBS_RAW)
    assert len(raw) == 1
    assert "Software Engineer" in decode(raw[0][1]).payload["job"]["title"]


def test_429_is_retried_with_retry_after(redis_client):
    source = make_source(id="acme")
    enqueue_task(redis_client, source)
    calls = {"count": 0}

    def responder(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=GREENHOUSE_PAYLOAD)

    source_store = FakeSourceStore()
    run_one(redis_client, httpx.MockTransport(responder), source_store)

    assert calls["count"] == 2
    assert source_store.successes == ["acme"]
    assert len(redis_client.xrange(streams.JOBS_RAW)) == 2


def test_permanent_http_error_dead_letters_task(redis_client):
    source = make_source(id="acme")
    enqueue_task(redis_client, source)
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    source_store = FakeSourceStore()

    run_one(redis_client, transport, source_store)

    assert redis_client.xlen(dlq_name(streams.CRAWL_TASKS)) == 1
    assert source_store.errors and source_store.errors[0][0] == "acme"
    assert redis_client.xpending(streams.CRAWL_TASKS, "crawlers")["pending"] == 0


def test_transient_server_error_leaves_task_pending(redis_client):
    source = make_source(id="acme")
    enqueue_task(redis_client, source)
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    source_store = FakeSourceStore()

    consumer = make_consumer(redis_client)
    message = consumer.read()[0]

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            await process_message(message, consumer, client, None, redis_client, source_store, asyncio.Semaphore(1))

    asyncio.run(go())

    assert redis_client.xlen(dlq_name(streams.CRAWL_TASKS)) == 0
    assert redis_client.xpending(streams.CRAWL_TASKS, "crawlers")["pending"] == 1


def test_malformed_source_payload_goes_to_dlq_not_crash(redis_client):
    # "source" is a string, not an object: must dead-letter, never crash the worker loop.
    streams.publish(
        redis_client,
        streams.CRAWL_TASKS,
        new_envelope(EVENT_CRAWL_TASK, {"source": "not-an-object"}),
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=GREENHOUSE_PAYLOAD))

    run_one(redis_client, transport, FakeSourceStore())

    assert redis_client.xlen(dlq_name(streams.CRAWL_TASKS)) == 1
    assert redis_client.xpending(streams.CRAWL_TASKS, "crawlers")["pending"] == 0


def test_lever_board_parses(redis_client):
    payload = [
        {
            "id": "abc",
            "text": "Backend Engineer",
            "categories": {"location": "Seattle, WA"},
            "descriptionPlain": "Python and Postgres.",
            "hostedUrl": "https://jobs.lever.co/acme/abc",
        }
    ]
    source = make_source(id="acme", atsType="lever", careerUrl="https://api.lever.co/v0/postings/{boardToken}?mode=json")
    jobs = parse_board(source, payload)
    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].source == "lever"
