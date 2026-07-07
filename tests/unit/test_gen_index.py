"""Regression guard for scripts/gen_index.py — the code-index generator.

The ``code-index-fresh`` pre-commit gate catches index-vs-code DRIFT, but nothing
otherwise stops a future edit from reintroducing NONdeterminism into the generator
(e.g. swapping ``sorted(...)`` for raw ``rglob`` order) — that would pass ``--check``
on the authoring machine and only flake for other developers/CI. These tests pin the
two properties the gate relies on: deterministic output, and ``--check`` failing on stale.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_GEN_PATH = Path(__file__).resolve().parents[2] / "scripts" / "gen_index.py"


def _load_gen_index():
    spec = importlib.util.spec_from_file_location("gen_index", _GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generator_output_is_deterministic():
    """Two consecutive builds must be byte-identical (else --check flakes across machines)."""
    gi = _load_gen_index()
    assert gi.build_global_index() == gi.build_global_index()
    assert gi.build_detailed_index() == gi.build_detailed_index()


def test_check_passes_when_current_and_fails_when_stale(tmp_path, monkeypatch):
    """--check returns 0 for a freshly-written index and non-zero once a target is mutated."""
    gi = _load_gen_index()
    global_index = tmp_path / "global_index.md"
    detailed_index = tmp_path / "detailed_index.md"
    monkeypatch.setattr(gi, "GLOBAL_INDEX", global_index)
    monkeypatch.setattr(gi, "DETAILED_INDEX", detailed_index)

    global_index.write_text(gi.build_global_index(), encoding="utf-8")
    detailed_index.write_text(gi.build_detailed_index(), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["gen_index.py", "--check"])
    assert gi.main() == 0

    global_index.write_text("STALE\n", encoding="utf-8")
    assert gi.main() == 1
