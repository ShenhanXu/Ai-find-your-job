from ingestion import streams
from ingestion.embedder import process_batch
from ingestion.envelope import EVENT_JOB_EMBED, new_envelope
from ingestion.streams import StreamConsumer, dlq_name

from .conftest import make_job


class FakeJobStore:
    def __init__(self, jobs_missing_embedding):
        self.jobs = {job.id: job for job in jobs_missing_embedding}
        self.embedded: list[tuple[str, str]] = []

    def fetch_for_embedding(self, job_ids):
        return [self.jobs[job_id] for job_id in job_ids if job_id in self.jobs]

    def set_embedding(self, job_id, fingerprint, vector_value):
        job = self.jobs.get(job_id)
        if job is None or job.fingerprint != fingerprint:
            return False
        self.embedded.append((job_id, fingerprint))
        return True


class FakeBatchProvider:
    source = "fake-batch"

    def __init__(self):
        self.batches: list[int] = []

    def embed_batch(self, texts):
        self.batches.append(len(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


class FailingProvider:
    source = "fake-failing"

    def embed_batch(self, texts):
        raise RuntimeError("provider down")


def make_consumer(redis_client) -> StreamConsumer:
    return StreamConsumer(redis_client, streams.JOBS_EMBED, "embedders", "test-embedder", batch=64, block_ms=1, claim_idle_ms=0)


def publish_task(redis_client, job_id, fingerprint):
    streams.publish(
        redis_client,
        streams.JOBS_EMBED,
        new_envelope(EVENT_JOB_EMBED, {"jobId": job_id, "fingerprint": fingerprint}),
    )


def test_batch_embeds_in_single_provider_call(redis_client):
    jobs = [make_job(job_id=f"acme-{i}", fingerprint=f"fp-{i}") for i in range(5)]
    store = FakeJobStore(jobs)
    provider = FakeBatchProvider()
    for job in jobs:
        publish_task(redis_client, job.id, job.fingerprint)

    consumer = make_consumer(redis_client)
    embedded = process_batch(consumer.read(), consumer, redis_client, store, provider)

    assert embedded == 5
    assert provider.batches == [5]  # one API request for the whole batch
    assert redis_client.xpending(streams.JOBS_EMBED, "embedders")["pending"] == 0


def test_already_embedded_and_superseded_tasks_are_acked_without_work(redis_client):
    current = make_job(job_id="acme-1", fingerprint="fp-new")
    store = FakeJobStore([current])
    provider = FakeBatchProvider()
    publish_task(redis_client, "acme-1", "fp-old")  # superseded: job re-fingerprinted since queued
    publish_task(redis_client, "acme-2", "fp-x")  # already embedded: not returned by fetch_for_embedding

    consumer = make_consumer(redis_client)
    embedded = process_batch(consumer.read(), consumer, redis_client, store, provider)

    assert embedded == 0
    assert provider.batches == []  # nothing sent to the provider
    assert redis_client.xpending(streams.JOBS_EMBED, "embedders")["pending"] == 0


def test_provider_failure_leaves_tasks_pending_for_retry(redis_client):
    job = make_job(job_id="acme-1", fingerprint="fp-1")
    store = FakeJobStore([job])
    publish_task(redis_client, "acme-1", "fp-1")

    consumer = make_consumer(redis_client)
    embedded = process_batch(consumer.read(), consumer, redis_client, store, FailingProvider())

    assert embedded == 0
    assert store.embedded == []
    assert redis_client.xpending(streams.JOBS_EMBED, "embedders")["pending"] == 1
    assert redis_client.xlen(dlq_name(streams.JOBS_EMBED)) == 0


def test_provider_failure_dead_letters_after_max_deliveries(redis_client):
    job = make_job(job_id="acme-1", fingerprint="fp-1")
    store = FakeJobStore([job])
    publish_task(redis_client, "acme-1", "fp-1")

    consumer = make_consumer(redis_client)
    messages = consumer.read()
    messages[0].delivery_count = consumer.max_deliveries

    process_batch(messages, consumer, redis_client, store, FailingProvider())

    assert redis_client.xlen(dlq_name(streams.JOBS_EMBED)) == 1
    assert redis_client.xpending(streams.JOBS_EMBED, "embedders")["pending"] == 0


def test_undecodable_task_goes_to_dlq(redis_client):
    redis_client.xadd(streams.JOBS_EMBED, {"data": "not json"})
    consumer = make_consumer(redis_client)

    process_batch(consumer.read(), consumer, redis_client, FakeJobStore([]), FakeBatchProvider())

    assert redis_client.xlen(dlq_name(streams.JOBS_EMBED)) == 1


class PoisonProvider:
    """Batch call fails; per-item fallback fails only for the poison job's text."""

    source = "fake-poison"

    def __init__(self, poison_text_marker):
        self.marker = poison_text_marker

    def embed_batch(self, texts):
        raise RuntimeError("HTTP 400: one input is invalid")

    def embed(self, text):
        if self.marker in text:
            raise RuntimeError("HTTP 400: invalid input")
        return [0.1, 0.2, 0.3]


def test_poison_job_fails_alone_not_the_whole_batch(redis_client):
    good = make_job(job_id="acme-good", fingerprint="fp-g")
    poison = make_job(job_id="acme-poison", fingerprint="fp-p", title="PoisonTitle")
    store = FakeJobStore([good, poison])
    for job in (good, poison):
        publish_task(redis_client, job.id, job.fingerprint)

    consumer = make_consumer(redis_client)
    embedded = process_batch(consumer.read(), consumer, redis_client, store, PoisonProvider("PoisonTitle"))

    assert embedded == 1
    assert [job_id for job_id, _ in store.embedded] == ["acme-good"]
    # Only the poison task stays pending; the good one is acked.
    assert redis_client.xpending(streams.JOBS_EMBED, "embedders")["pending"] == 1


def test_rate_limited_batch_is_not_retried_per_item(redis_client):
    calls = {"per_item": 0}

    class RateLimitedProvider:
        source = "fake-429"

        def embed_batch(self, texts):
            raise RuntimeError("HTTP 429: token rate limit exceeded")

        def embed(self, text):
            calls["per_item"] += 1
            return [0.0]

    job = make_job(job_id="acme-1", fingerprint="fp-1")
    store = FakeJobStore([job])
    publish_task(redis_client, "acme-1", "fp-1")

    consumer = make_consumer(redis_client)
    embedded = process_batch(consumer.read(), consumer, redis_client, store, RateLimitedProvider())

    assert embedded == 0
    assert calls["per_item"] == 0  # no per-item hammering while rate limited
    assert redis_client.xpending(streams.JOBS_EMBED, "embedders")["pending"] == 1


def test_successful_embed_releases_dedup_key(redis_client):
    job = make_job(job_id="acme-1", fingerprint="fp-1")
    store = FakeJobStore([job])
    redis_client.set("ingest:dedup:embed:acme-1:fp-1", "1")
    publish_task(redis_client, "acme-1", "fp-1")

    consumer = make_consumer(redis_client)
    process_batch(consumer.read(), consumer, redis_client, store, FakeBatchProvider())

    assert redis_client.get("ingest:dedup:embed:acme-1:fp-1") is None


def test_provider_failure_still_acks_no_work_tasks(redis_client):
    needs_work = make_job(job_id="acme-1", fingerprint="fp-1")
    store = FakeJobStore([needs_work])
    publish_task(redis_client, "acme-1", "fp-1")
    publish_task(redis_client, "acme-done", "fp-x")  # already embedded: no work

    consumer = make_consumer(redis_client)
    process_batch(consumer.read(), consumer, redis_client, store, FailingProvider())

    # Only the task that actually needed work is held for retry.
    assert redis_client.xpending(streams.JOBS_EMBED, "embedders")["pending"] == 1
