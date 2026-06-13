"""Unit tests for the conserved-sites ALIGN cache (E3-9).

Covers the cache contract WITHOUT MAFFT or BV-BRC:
  * the key is stable for identical inputs and DIFFERS when aligner / mode / amino / executable /
    sequence-hash changes (the G24 content-hash of the FASTA is a real determinant);
  * a hit returns the stored payload; a missing or corrupt entry → recompute (None);
  * the NOCACHE escape hatch is read live;
  * the step wires it: process() run-1 calls MAFFT once + writes; run-2 HITs (no MAFFT call) and
    returns a byte-identical result with the live payload's context re-applied.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps import _align_cache
from apecx_integration.composition.steps.local_mafft_align_step import LocalMafftAlignStep

_FASTA_A = ">a\nMKTAYIAK\n>b\nMKTAYIAQ\n>c\nMKTAYIDK\n"
_FASTA_B = ">a\nMKTAYIAK\n>b\nMKTAYIAQ\n>c\nMKTAYIDR\n"  # one residue differs → different bytes


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_CONSERVED_SITES_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("APECX_CONSERVED_SITES_NOCACHE", raising=False)


def _key(**over) -> str:
    base = {
        "aligner": "mafft",
        "mode": "--auto",
        "amino": True,
        "executable": "mafft",
        "fasta_text": _FASTA_A,
    }
    base.update(over)
    return _align_cache.align_cache_key(**base)


# --------------------------------------------------------------------------- #
# key stability + sensitivity
# --------------------------------------------------------------------------- #
def test_key_stable_for_identical_inputs():
    assert _key() == _key()


def test_key_differs_on_aligner():
    assert _key() != _key(aligner="muscle")


def test_key_differs_on_mode():
    assert _key() != _key(mode="--maxiterate 1000")


def test_key_differs_on_amino():
    assert _key() != _key(amino=False)


def test_key_differs_on_executable():
    assert _key() != _key(executable="/opt/homebrew/bin/mafft")


def test_key_differs_on_sequence_hash():
    # A single-residue corpus change → different FASTA bytes → different G24 content-hash → MISS.
    assert _key() != _key(fasta_text=_FASTA_B)


def test_sequence_hash_uses_g24_and_is_content_addressed():
    h1 = _align_cache.compute_sequence_set_hash(_FASTA_A)
    h2 = _align_cache.compute_sequence_set_hash(_FASTA_A)
    h3 = _align_cache.compute_sequence_set_hash(_FASTA_B)
    assert h1 == h2 and h1 != h3
    assert len(h1) == 64  # sha256 hex


# --------------------------------------------------------------------------- #
# read / write / corrupt / nocache
# --------------------------------------------------------------------------- #
def test_hit_returns_stored_payload():
    k = _key()
    assert _align_cache.read_cached(k) is None  # cold miss
    _align_cache.write_cached(k, {"alignment_fasta": ">x\nMK\n", "n_sequences": 1})
    assert _align_cache.read_cached(k) == {"alignment_fasta": ">x\nMK\n", "n_sequences": 1}


def test_missing_entry_is_a_miss():
    assert _align_cache.read_cached("deadbeef") is None


def test_corrupt_entry_degrades_to_miss(tmp_path):
    k = _key()
    _align_cache.write_cached(k, {"ok": 1})
    # Corrupt the file on disk → read must NOT raise, returns None (recompute path).
    path = Path(_align_cache._entry_path(k))
    path.write_text("{ this is not valid json ")
    assert _align_cache.read_cached(k) is None


def test_nocache_env_is_read_live(monkeypatch):
    assert _align_cache.nocache_enabled() is False
    monkeypatch.setenv("APECX_CONSERVED_SITES_NOCACHE", "1")
    assert _align_cache.nocache_enabled() is True


# --------------------------------------------------------------------------- #
# step-level wiring (no real MAFFT — _run_mafft is monkeypatched)
# --------------------------------------------------------------------------- #
def _stage(tmp_path: Path) -> LocalMafftAlignStep:
    p = tmp_path / "mafft.yml"
    p.write_text("name: mafft_test\n")
    return LocalMafftAlignStep.from_config(str(p))


_ALIGNED = ">a\nMKTAYIAK\n>b\nMKTAYIAQ\n>c\nMKTAYIDK\n"


def test_step_run1_aligns_run2_hits_byte_identical(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    calls = {"n": 0}

    def _fake_mafft(fasta_text):
        calls["n"] += 1
        return _ALIGNED

    monkeypatch.setattr(step, "_run_mafft", _fake_mafft)
    monkeypatch.setattr(step, "_mafft_version", lambda: "v7.526 (fake)")

    payload = {"fasta_text": _FASTA_A, "taxon_id": 37124, "protein": "E1"}
    out1 = asyncio.run(step.process(dict(payload)))
    out2 = asyncio.run(step.process(dict(payload)))

    assert calls["n"] == 1, "run-2 must HIT the cache and NOT call MAFFT again"
    assert out1 == out2, "a cache HIT must be byte-identical to the fresh run (CC-4)"
    assert out1["alignment"]["alignment_fasta"] == _ALIGNED
    assert out1["alignment"]["taxon_id"] == 37124


def test_step_nocache_forces_recompute(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(
        step, "_run_mafft", lambda f: calls.__setitem__("n", calls["n"] + 1) or _ALIGNED
    )
    monkeypatch.setattr(step, "_mafft_version", lambda: "v")
    monkeypatch.setenv("APECX_CONSERVED_SITES_NOCACHE", "1")

    payload = {"fasta_text": _FASTA_A, "taxon_id": 1, "protein": "X"}
    asyncio.run(step.process(dict(payload)))
    asyncio.run(step.process(dict(payload)))
    assert calls["n"] == 2, "NOCACHE must force a recompute on every run"


def test_step_hit_reapplies_live_context(tmp_path, monkeypatch):
    # Same sequences, different protein label on run-2 → the HIT carries the LIVE label,
    # not the stored one (guarantees HIT == FRESH for the current payload).
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_run_mafft", lambda f: _ALIGNED)
    monkeypatch.setattr(step, "_mafft_version", lambda: "v")

    asyncio.run(step.process({"fasta_text": _FASTA_A, "taxon_id": 37124, "protein": "E1"}))
    out2 = asyncio.run(step.process({"fasta_text": _FASTA_A, "taxon_id": 37124, "protein": "E2"}))
    assert out2["alignment"]["protein"] == "E2"
