"""CW-MEM5 — unit tests for MemoryStore (pure-Python, no LLM).

Pins:
  1. Empty store returns [] for any read.
  2. Write creates the directory tree + atomic file with stable schema.
  3. Re-write same lesson (restatement) is skipped by default.
  4. Re-write distinct lesson lands.
  5. Read for spec_id returns newest-first, bounded by limit.
  6. Read by keywords falls back when no spec_id match.
  7. Lesson < min_lesson_chars is skipped silently (returns None).
  8. Slugify handles spaces / special chars deterministically.
  9. Schema-version bump is rejected (forward-compat fail-fast).
 10. Atomic-write: temp file does not survive a successful commit.
"""

from __future__ import annotations

import json

import pytest

from apecx_integration.composition.steps.memory_store import (
    MEMORY_SCHEMA_VERSION,
    MemoryEntry,
    MemoryStore,
    _slugify,
)


def test_empty_store_returns_empty_reads(tmp_path):
    store = MemoryStore(root=tmp_path)
    assert store.read_for_spec("never_seen") == []
    assert store.read_by_keywords(spec_keywords=["x", "y"]) == []
    assert store.all_entries() == []


def test_write_creates_file_with_expected_schema(tmp_path):
    store = MemoryStore(root=tmp_path)
    path = store.write(
        spec_id="fizzbuzz_v1",
        attempt_n=1,
        status="fail",
        lesson="The function did not raise on n<1; spec demanded ValueError.",
        failure_keywords=["valueerror", "missing_guard"],
        spec_keywords=["fizzbuzz", "modulo"],
        metadata={"function_name": "fizzbuzz"},
    )
    assert path is not None
    assert path.exists()
    raw = json.loads(path.read_text())
    assert raw["memory_schema_version"] == MEMORY_SCHEMA_VERSION
    assert raw["spec_id"] == "fizzbuzz_v1"
    assert raw["status"] == "fail"
    assert raw["lesson"].startswith("The function did not raise")
    assert "valueerror" in raw["failure_keywords"]
    assert raw["metadata"]["function_name"] == "fizzbuzz"


def test_skip_if_restatement_default(tmp_path):
    """Same lesson + same keywords → second write is skipped."""
    store = MemoryStore(root=tmp_path)
    lesson = (
        "Off-by-one error in fizzbuzz: the spec asks 1..n but the "
        "function loops 0..n-1. Fix the loop bounds."
    )
    p1 = store.write(
        spec_id="fizzbuzz_v1",
        attempt_n=1,
        status="fail",
        lesson=lesson,
        spec_keywords=["fizzbuzz", "loop_bounds"],
        failure_keywords=["off_by_one"],
    )
    assert p1 is not None
    p2 = store.write(
        spec_id="fizzbuzz_v1",
        attempt_n=2,
        status="fail",
        lesson=lesson,  # restatement
        spec_keywords=["fizzbuzz", "loop_bounds"],
        failure_keywords=["off_by_one"],
    )
    assert p2 is None
    # Only the first entry persists.
    entries = store.read_for_spec("fizzbuzz_v1")
    assert len(entries) == 1


def test_distinct_lesson_lands_even_with_overlapping_keywords(tmp_path):
    import time as _t

    store = MemoryStore(root=tmp_path)
    store.write(
        spec_id="fizzbuzz_v1",
        attempt_n=1,
        status="fail",
        lesson="The function did not raise on n<1; spec demanded ValueError.",
        spec_keywords=["fizzbuzz"],
        failure_keywords=["valueerror"],
    )
    # Timestamp-token granularity is 1 second; pause so the second
    # entry gets a distinct filename.
    _t.sleep(1.05)
    p2 = store.write(
        spec_id="fizzbuzz_v1",
        attempt_n=2,
        status="pass",
        lesson="Adding the n<1 guard let the function pass all spec assertions cleanly.",
        spec_keywords=["fizzbuzz"],
        failure_keywords=[],
    )
    assert p2 is not None
    entries = store.read_for_spec("fizzbuzz_v1")
    assert len(entries) == 2


def test_read_for_spec_is_newest_first(tmp_path):
    store = MemoryStore(root=tmp_path)
    # Distinct lessons so neither is rejected as restatement.
    import time as _t

    store.write(
        spec_id="fizzbuzz_v1",
        attempt_n=1,
        status="fail",
        lesson="First attempt failed because base case missed n=0.",
    )
    _t.sleep(0.01)  # Ensure distinct timestamp tokens (microsecond precision).
    store.write(
        spec_id="fizzbuzz_v1",
        attempt_n=2,
        status="pass",
        lesson="Adding explicit base case for n=0 resolved the previous failure.",
    )
    entries = store.read_for_spec("fizzbuzz_v1", limit=10)
    assert len(entries) == 2
    # Newest first.
    assert entries[0].attempt_n == 2
    assert entries[1].attempt_n == 1


def test_read_for_spec_respects_limit(tmp_path):
    store = MemoryStore(root=tmp_path)
    import time as _t

    # Each lesson must be lexically distinct enough to survive the
    # restatement gate (lesson Jaccard ≤ 0.7).
    distinct_lessons = [
        "Modulo arithmetic broke when negative inputs landed; need an isinstance guard.",
        "Concatenation order produced reversed output; rewrote the join statement.",
        "Recursion depth exceeded for inputs above twenty thousand; switched to a loop.",
        "Type annotation said int but the caller passed str; added explicit cast.",
        "Empty dict default mutated across calls; replaced with None plus copy.",
    ]
    for i, lesson in enumerate(distinct_lessons):
        store.write(
            spec_id="proj_a",
            attempt_n=i + 1,
            status="fail",
            lesson=lesson,
        )
        _t.sleep(0.01)
    entries = store.read_for_spec("proj_a", limit=2)
    assert len(entries) == 2


def test_read_by_keywords_jaccard_fallback(tmp_path):
    """When no spec_id matches, keyword-Jaccard retrieval kicks in."""
    store = MemoryStore(root=tmp_path)
    store.write(
        spec_id="proj_a",
        attempt_n=1,
        status="fail",
        lesson="Some failure lesson about the fizz pattern that is suitably long.",
        spec_keywords=["fizz", "modulo"],
    )
    # No entry for proj_b, but keyword overlap with "fizz".
    matches = store.read_by_keywords(spec_keywords=["fizz", "extra"])
    assert len(matches) == 1
    assert matches[0].spec_id == "proj_a"


def test_lesson_below_min_chars_silently_skipped(tmp_path):
    """Low-signal lessons are dropped without error."""
    store = MemoryStore(root=tmp_path)
    p = store.write(
        spec_id="proj_a",
        attempt_n=1,
        status="fail",
        lesson="short.",
    )
    assert p is None
    assert store.read_for_spec("proj_a") == []


def test_slugify_handles_spaces_and_specials():
    assert _slugify("fizz buzz v1") == "fizz_buzz_v1"
    assert _slugify("My Spec! v2.0") == "my_spec_v2.0"
    assert _slugify("") == "anonymous"
    assert _slugify("   ") == "anonymous"


def test_schema_version_too_new_raises_on_read(tmp_path):
    """Forward-compat: a memory file with a newer schema version
    raises rather than getting silently misread."""
    store = MemoryStore(root=tmp_path)
    # Write a normal entry first to materialize directory layout.
    p = store.write(
        spec_id="proj_a",
        attempt_n=1,
        status="fail",
        lesson="A normal lesson that is long enough to pass the gate.",
    )
    assert p is not None
    # Manually corrupt the schema version.
    raw = json.loads(p.read_text())
    raw["memory_schema_version"] = 999
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="schema_version=999"):
        store._load(p)


def test_atomic_write_leaves_no_tmp_file_on_success(tmp_path):
    store = MemoryStore(root=tmp_path)
    store.write(
        spec_id="proj_a",
        attempt_n=1,
        status="fail",
        lesson="A long enough lesson that survives the min-chars gate.",
    )
    # After a successful write, no .tmp file lingers.
    leftovers = list(tmp_path.rglob("*.tmp"))
    assert leftovers == []


def test_memory_entry_round_trips_through_to_dict_from_dict():
    e1 = MemoryEntry(
        spec_id="x",
        attempt_n=1,
        status="pass",
        lesson="a long enough lesson to survive any min-chars filter at write time.",
        failure_keywords=("k1", "k2"),
        spec_keywords=("a", "b"),
        created_at="2026-05-12T00:00:00+00:00",
        source_commit="abc1234",
        metadata={"f": 1},
        id="2026-05-12T00-00-00Z-1",
    )
    d = e1.to_dict()
    e2 = MemoryEntry.from_dict(d)
    assert e2 == e1
