"""Unit tests for the canonical data shapes (EO-12)."""

import pytest
from pydantic import ValidationError

from apecx_integration.composition.schemas.data_shapes import (
    Artifact,
    Bundle,
    Evidence,
    EvidenceItem,
    RecordSet,
    parse_data_shape,
)


def test_record_set_preview():
    rs = RecordSet(records=[{"id": i} for i in range(10)], columns=["id"])
    p = rs.preview(limit=2)
    assert p["kind"] == "record_set"
    assert p["count"] == 10
    assert p["columns"] == ["id"]
    assert p["sample"] == [{"id": 0}, {"id": 1}]


def test_evidence_preview():
    ev = Evidence(items=[EvidenceItem(claim="c", source="s", score=0.9)])
    p = ev.preview()
    assert p["kind"] == "evidence"
    assert p["count"] == 1
    assert p["sample"][0]["claim"] == "c"


def test_bundle_preview():
    b = Bundle(parts={"violin": [1], "pubmed": [2]})
    assert b.preview()["parts"] == ["pubmed", "violin"]


def test_artifact_preview():
    a = Artifact(uri="file:///tmp/x.png", media_type="image/png", size_bytes=42)
    p = a.preview()
    assert p["uri"].endswith("x.png")
    assert p["size_bytes"] == 42


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        RecordSet(records=[], oops=1)


def test_parse_discriminated():
    parsed = parse_data_shape({"kind": "record_set", "records": [{"a": 1}]})
    assert isinstance(parsed, RecordSet)
    assert parsed.records == [{"a": 1}]


def test_parse_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        parse_data_shape({"kind": "not_a_shape"})


def test_parse_missing_kind_rejected():
    with pytest.raises(ValidationError):
        parse_data_shape({"records": []})


def test_roundtrip():
    ev = Evidence(items=[EvidenceItem(claim="c", source="s")])
    dumped = ev.model_dump(mode="json")
    assert dumped["kind"] == "evidence"
    assert parse_data_shape(dumped) == ev
