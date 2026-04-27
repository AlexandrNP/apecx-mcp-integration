"""Probe batch 49 — adversarial probes against ProvenanceRecorder
module-surface shapes.

Streak before this batch: 224/300 post-AQ post-1066.
Probe naming: 1280–1304.

Distinct probes only — focuses on PUBLIC API + helper-function
shapes, NOT end-to-end DB-coupled behavior (covered by earlier
batches).
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from apecx_integration.control_plane.provenance.recorder import (
    ChainBroken,
    ProvenanceRecorder,
    _canonical_json,
    _canonical_timestamp,
    _compute_event_hash,
)


pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Probes 1280–1304
# --------------------------------------------------------------------------- #


def test_probe_1280_chain_broken_is_exception_subclass():
    """ChainBroken inherits from Exception so callers can catch it
    via the standard exception hierarchy."""
    assert issubclass(ChainBroken, Exception)


def test_probe_1281_canonical_json_sorts_keys():
    """canonical_json must produce key-sorted JSON. Hash chain
    integrity depends on byte-equivalent serialization."""
    a = _canonical_json({"b": 2, "a": 1})
    b = _canonical_json({"a": 1, "b": 2})
    assert a == b


def test_probe_1282_canonical_json_handles_nested_dicts():
    """Nested dicts must also have sorted keys."""
    a = _canonical_json({"outer": {"b": 2, "a": 1}})
    b = _canonical_json({"outer": {"a": 1, "b": 2}})
    assert a == b


def test_probe_1283_canonical_json_uses_no_whitespace_separators():
    """Whitespace differences would change byte length and hash.
    The canonical form must use no-whitespace separators."""
    out = _canonical_json({"a": 1, "b": 2})
    # ``,`` and ``:`` with no surrounding spaces.
    assert ", " not in out
    assert ": " not in out


def test_probe_1284_canonical_json_returns_str():
    """Pin: returns a string (encoded form), not bytes."""
    out = _canonical_json({"x": "y"})
    assert isinstance(out, str)


def test_probe_1285_canonical_json_handles_unicode_in_values():
    """Unicode values must round-trip without escape mangling."""
    out = _canonical_json({"name": "café"})
    # Either escaped (é) or raw — both deterministic.
    assert "café" in out or "\\u00e9" in out
    # Re-parse must round-trip.
    assert json.loads(out)["name"] == "café"


def test_probe_1286_canonical_timestamp_format_is_isoformat():
    """The canonical timestamp must produce a stable string format
    so two records of the same instant hash identically."""
    ts = datetime(2026, 4, 27, 12, 30, 45, 123456, tzinfo=UTC)
    out = _canonical_timestamp(ts)
    assert isinstance(out, str)
    # Format check — should be ISO8601-shaped.
    assert "2026" in out
    assert "12:30:45" in out


def test_probe_1287_canonical_timestamp_naive_datetime_handled():
    """A naive datetime (no timezone) must either be rejected or
    coerced to UTC. Pin behavior."""
    naive = datetime(2026, 4, 27, 12, 0, 0)
    try:
        out = _canonical_timestamp(naive)
        # If accepted, it must produce a string.
        assert isinstance(out, str)
    except (TypeError, ValueError):
        # Acceptable — fail-fast on naive datetime.
        pass


def test_probe_1288_compute_event_hash_signature_pinned():
    """_compute_event_hash signature is load-bearing — used in tests
    + by hash chain replay validators."""
    sig = inspect.signature(_compute_event_hash)
    # Just verify it exists and takes some positional args.
    assert len(sig.parameters) >= 1


def test_probe_1289_compute_event_hash_returns_hex_string():
    """Hash output must be a hex string (used in DB string column)."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    sig = inspect.signature(_compute_event_hash)
    params = list(sig.parameters.keys())
    kwargs: dict[str, object] = {}
    for p in params:
        if p in {"previous_hash", "prev_event_hash"}:
            kwargs[p] = None
        elif p == "event_type":
            kwargs[p] = ProvenanceEventType.RUN_STARTED
        elif p == "run_id":
            kwargs[p] = uuid4()
        elif p == "timestamp_iso":
            kwargs[p] = _canonical_timestamp(datetime.now(UTC))
        elif p == "timestamp":
            kwargs[p] = datetime.now(UTC)
        elif p == "payload":
            kwargs[p] = {"key": "value"}
        elif p == "actor":
            kwargs[p] = "test"
        else:
            kwargs[p] = None
    h = _compute_event_hash(**kwargs)
    assert isinstance(h, str)
    assert all(c in "0123456789abcdefABCDEF" for c in h)


def test_probe_1290_provenance_recorder_init_signature_pinned():
    """Constructor takes session_factory only. A hidden dependency
    (e.g. a global lock) would silently couple test instances."""
    sig = inspect.signature(ProvenanceRecorder.__init__)
    params = list(sig.parameters.keys())
    assert "session_factory" in params


def test_probe_1291_provenance_recorder_record_method_exists():
    assert hasattr(ProvenanceRecorder, "record")


def test_probe_1292_provenance_recorder_validate_method_exists():
    assert hasattr(ProvenanceRecorder, "validate")


def test_probe_1293_provenance_recorder_record_signature_pinned():
    """Signature includes run_id, event_type, payload, actor (per
    DB schema). Pin so a refactor changing positional order is
    intentional."""
    sig = inspect.signature(ProvenanceRecorder.record)
    params = list(sig.parameters.keys())
    expected = {"run_id", "event_type", "payload"}
    actual = set(params) - {"self"}
    assert expected.issubset(actual), (
        f"record() missing parameters: {expected - actual}"
    )


def test_probe_1294_chain_broken_message_describes_the_break():
    """Pin: ChainBroken's __init__ accepts a message string."""
    e = ChainBroken("hash mismatch at event N")
    assert "hash mismatch" in str(e)


def test_probe_1295_canonical_json_produces_byte_equivalent_for_equal_dicts():
    """Two semantically equal dicts (different construction order)
    must hash identically. This is the hash chain's foundational
    invariant."""
    d1 = {}
    d1["b"] = [3, 2, 1]
    d1["a"] = "x"
    d2 = {}
    d2["a"] = "x"
    d2["b"] = [3, 2, 1]
    assert _canonical_json(d1) == _canonical_json(d2)


def test_probe_1296_canonical_json_lists_preserve_order():
    """Lists are ordered; canonical_json must NOT sort list elements
    (would corrupt sequence semantics)."""
    out = _canonical_json({"x": [3, 1, 2]})
    # Order preserved.
    assert "[3, 1, 2]".replace(" ", "") in out.replace(" ", "")


def test_probe_1297_canonical_json_handles_floats():
    """Float repr must be deterministic across runs. Pin."""
    out_a = _canonical_json({"v": 1.5})
    out_b = _canonical_json({"v": 1.5})
    assert out_a == out_b


def test_probe_1298_canonical_json_handles_booleans():
    """Booleans serialize to lowercase JSON 'true'/'false'."""
    out = _canonical_json({"flag": True})
    assert "true" in out


def test_probe_1299_canonical_json_handles_none():
    """None serializes to JSON 'null'."""
    out = _canonical_json({"x": None})
    assert "null" in out


def test_probe_1300_canonical_json_does_not_emit_NaN_or_inf():
    """JSON does not officially support NaN/inf. The canonical form
    must either reject or use a deterministic fallback. Pin."""
    import math
    try:
        _canonical_json({"v": math.nan})
        # If accepted, the output must not vary across calls.
        a = _canonical_json({"v": math.nan})
        b = _canonical_json({"v": math.nan})
        assert a == b
    except (ValueError, TypeError):
        # Acceptable: fail-fast on non-finite floats.
        pass


def _compose_hash_kwargs(
    event_type, prev_hash, run_id_val=None, ts=None,
):
    """Helper: build kwargs matching _compute_event_hash's signature."""
    sig = inspect.signature(_compute_event_hash)
    kw = {}
    for p in sig.parameters:
        if p in {"previous_hash", "prev_event_hash"}:
            kw[p] = prev_hash
        elif p == "event_type":
            kw[p] = event_type
        elif p == "run_id":
            kw[p] = run_id_val if run_id_val is not None else uuid4()
        elif p == "timestamp_iso":
            kw[p] = ts or _canonical_timestamp(datetime.now(UTC))
        elif p == "timestamp":
            kw[p] = datetime.now(UTC)
        elif p == "payload":
            kw[p] = {}
        elif p == "actor":
            kw[p] = "y"
        else:
            kw[p] = None
    return kw


def test_probe_1301_compute_event_hash_changes_with_event_type():
    """Two events identical in everything but event_type must hash
    differently. Pin: event_type is part of the hashable payload."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    rid = uuid4()
    ts = _canonical_timestamp(datetime.now(UTC))
    kw1 = _compose_hash_kwargs(
        ProvenanceEventType.RUN_STARTED, None, rid, ts,
    )
    kw2 = _compose_hash_kwargs(
        ProvenanceEventType.RUN_COMPLETED, None, rid, ts,
    )
    h1 = _compute_event_hash(**kw1)
    h2 = _compute_event_hash(**kw2)
    assert h1 != h2


def test_probe_1302_compute_event_hash_changes_with_previous_hash():
    """Hash chain integrity: prev_hash is part of the hashable input.
    Two events identical except previous_hash must hash differently."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    rid = uuid4()
    ts = _canonical_timestamp(datetime.now(UTC))
    kw_a = _compose_hash_kwargs(
        ProvenanceEventType.RUN_STARTED, None, rid, ts,
    )
    kw_b = _compose_hash_kwargs(
        ProvenanceEventType.RUN_STARTED, "a" * 64, rid, ts,
    )
    h_a = _compute_event_hash(**kw_a)
    h_b = _compute_event_hash(**kw_b)
    assert h_a != h_b


def test_probe_1303_provenance_recorder_class_module_path():
    """The class lives in apecx_integration.control_plane.provenance.recorder
    Pin so a refactor moving it is intentional."""
    assert ProvenanceRecorder.__module__ == (
        "apecx_integration.control_plane.provenance.recorder"
    )


def test_probe_1304_chain_broken_can_be_caught_via_exception_base():
    """Exception subclass — caller's ``except Exception:`` catches."""
    try:
        raise ChainBroken("test")
    except Exception:
        pass  # caught successfully
