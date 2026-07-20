import json

import pytest

from ingestion.envelope import (
    SCHEMA_VERSION,
    EnvelopeError,
    SchemaVersionError,
    decode,
    encode,
    new_envelope,
)


def test_roundtrip():
    envelope = new_envelope("job.raw", {"job": {"id": "x"}})
    decoded = decode(encode(envelope))
    assert decoded.type == "job.raw"
    assert decoded.payload == {"job": {"id": "x"}}
    assert decoded.schema_version == SCHEMA_VERSION
    assert decoded.event_id == envelope.event_id


def test_future_schema_version_is_rejected():
    fields = {
        "data": json.dumps({"schema_version": SCHEMA_VERSION + 1, "type": "job.raw", "payload": {}})
    }
    with pytest.raises(SchemaVersionError):
        decode(fields)


def test_additive_fields_are_tolerated():
    fields = {
        "data": json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "type": "job.raw",
                "payload": {"job": {"id": "x"}, "salaryRange": "100k-150k"},
                "brand_new_envelope_key": True,
            }
        )
    }
    decoded = decode(fields)
    assert decoded.payload["salaryRange"] == "100k-150k"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"data": "not json"},
        {"data": json.dumps(["not", "an", "object"])},
        {"data": json.dumps({"schema_version": 1, "payload": {}})},
        {"data": json.dumps({"schema_version": 1, "type": "job.raw"})},
    ],
)
def test_malformed_messages_raise(fields):
    with pytest.raises(EnvelopeError):
        decode(fields)
