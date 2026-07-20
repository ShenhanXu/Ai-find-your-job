from ingestion import streams
from ingestion.envelope import EVENT_CRAWL_TASK, decode
from ingestion.scheduler import dedup_key, enqueue_due

from .conftest import make_source


class FakeSourceStore:
    def __init__(self, sources):
        self.sources = sources
        self.enqueued: list[str] = []

    def due_sources(self, ats_types):
        return [source for source in self.sources if source.ats_type in ats_types]

    def mark_enqueued(self, source_ids):
        self.enqueued.extend(source_ids)


def test_enqueues_due_sources_once_per_window(redis_client):
    store = FakeSourceStore([make_source(id="acme"), make_source(id="beta", boardToken="beta")])
    now = 1_700_000_000.0

    first = enqueue_due(redis_client, store, ("greenhouse", "lever"), now_ts=now)
    assert first == ["acme", "beta"]
    assert store.enqueued == ["acme", "beta"]
    assert redis_client.xlen(streams.CRAWL_TASKS) == 2

    # Same window (e.g. scheduler restart, or a second replica): nothing new.
    second = enqueue_due(redis_client, store, ("greenhouse", "lever"), now_ts=now + 10)
    assert second == []
    assert redis_client.xlen(streams.CRAWL_TASKS) == 2

    # Next window: enqueued again.
    third = enqueue_due(redis_client, store, ("greenhouse", "lever"), now_ts=now + 31 * 60)
    assert third == ["acme", "beta"]
    assert redis_client.xlen(streams.CRAWL_TASKS) == 4


def test_task_payload_carries_full_source_config(redis_client):
    store = FakeSourceStore([make_source(id="acme", roleKeywords=["backend"])])
    enqueue_due(redis_client, store, ("greenhouse",), now_ts=1_700_000_000.0)

    _, fields = redis_client.xrange(streams.CRAWL_TASKS)[0]
    envelope = decode(fields)
    assert envelope.type == EVENT_CRAWL_TASK
    assert envelope.payload["source"]["id"] == "acme"
    assert envelope.payload["source"]["roleKeywords"] == ["backend"]


def test_dedup_key_changes_with_window():
    early = dedup_key("acme", 30, 1_700_000_000.0)
    same_window = dedup_key("acme", 30, 1_700_000_000.0 + 60)
    next_window = dedup_key("acme", 30, 1_700_000_000.0 + 31 * 60)
    assert early == same_window
    assert early != next_window


def test_filters_by_ats_type(redis_client):
    store = FakeSourceStore([make_source(id="acme"), make_source(id="html-co", atsType="generic_html")])
    enqueued = enqueue_due(redis_client, store, ("greenhouse", "lever"), now_ts=1_700_000_000.0)
    assert enqueued == ["acme"]


def test_failed_publish_releases_window_claim(redis_client, monkeypatch):
    """If the XADD fails after the SET NX claim, the claim must be undone —
    otherwise the source silently skips the entire interval window."""
    import ingestion.scheduler as scheduler_module

    store = FakeSourceStore([make_source(id="acme")])
    now = 1_700_000_000.0

    def broken_publish(*args, **kwargs):
        raise ConnectionError("redis hiccup")

    monkeypatch.setattr(scheduler_module.streams, "publish", broken_publish)
    try:
        enqueue_due(redis_client, store, ("greenhouse",), now_ts=now)
    except ConnectionError:
        pass

    monkeypatch.undo()
    enqueued = enqueue_due(redis_client, store, ("greenhouse",), now_ts=now)
    assert enqueued == ["acme"]  # same window, claim was released, so it retries
