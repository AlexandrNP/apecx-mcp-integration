"""Unit tests for RheaGenomicAnalysisStep — the MANDATORY-but-degrade-loud RHEA conservation leg.

RHEA genomic-analysis is a mandatory PART of the analysis (always attempted + DISCLOSED), but its
absence DEGRADES LOUD — it does NOT fail the run. A missing taxon/protein or a RHEA runtime failure
produces a prominent warning + honest autodeploy fix instructions (a named note + a proceed_notes
entry) and the bundle passes through so the rest of the analysis still completes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.rhea_genomic_analysis_step import (
    RheaGenomicAnalysisStep,
)


def _stage(tmp_path: Path, **cfg) -> RheaGenomicAnalysisStep:
    p = tmp_path / "rhea_genomic.yml"
    body = "name: rhea_genomic_test\n" + "".join(f"{k}: {v}\n" for k, v in cfg.items())
    p.write_text(body)
    return RheaGenomicAnalysisStep.from_config(str(p))


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path, timeout_seconds=120)
    assert step.name == "rhea_genomic_test"
    assert step._timeout == 120.0


def _assert_loud_unavailable(out: dict) -> None:
    note = out["rhea_conservation_note"]
    assert note and "not available" in note.lower(), note
    # honest autodeploy fix instructions (no dead `apecx-setup rhea` command)
    assert "docker" in note.lower() and "rhea source" in note.lower(), note
    assert "apecx-setup rhea" not in note, note
    assert "still" in note.lower() or "remains valid" in note.lower(), note  # don't-fail framing
    # a loud "how to proceed" entry is appended for prominence
    pn = out.get("proceed_notes") or []
    assert any("rhea" in (n.get("stage", "") + n.get("what", "")).lower() for n in pn), pn


def test_missing_taxon_degrades_loud(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"query": "chikv", "protein": "E1"}))
    assert out["rhea_conservation"] is None
    assert out["query"] == "chikv"  # bundle passed through (did not fail)
    _assert_loud_unavailable(out)


def test_missing_protein_degrades_loud(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"query": "chikv", "taxon_id": 37124}))
    assert out["rhea_conservation"] is None
    _assert_loud_unavailable(out)


def test_rhea_failure_degrades_loud_not_raises(tmp_path, monkeypatch):
    """A failure inside the RHEA drive becomes a loud warning + fix instructions, NOT a raise.

    The note is produced by the honest _diagnose_rhea_failure probe, so we satisfy both prereqs
    (client importable + server reachable) to drive the tool-failed branch, which surfaces the
    underlying exception. The missing-prereq branches are covered by test_rhea_failure_diagnosis.py.
    """
    import sys
    import types

    step = _stage(tmp_path)

    async def _boom(taxon_id, protein):
        raise RuntimeError("Rhea server unreachable")

    monkeypatch.setattr(step, "_drive_rhea_conservation", _boom)
    for name in ("rhea", "rhea.utils", "rhea.utils.proxy"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    from apecx_integration.infrastructure import probes

    async def _healthy(*, mcp_url, timeout_s=5.0):
        return types.SimpleNamespace(healthy=True, detail="ok", error=None)

    monkeypatch.setattr(probes, "rhea_mcp_probe", _healthy)

    out = asyncio.run(step.process({"query": "chikv", "taxon_id": 37124, "protein": "E1"}))
    assert out["rhea_conservation"] is None
    assert "RuntimeError" in out["rhea_conservation_note"]
    _assert_loud_unavailable(out)


def test_success_folds_conservation(tmp_path, monkeypatch):
    step = _stage(tmp_path)

    async def _ok(taxon_id, protein):
        return {
            "markdown": "# Conserved sites\n...",
            "data": {
                "parts": {
                    "conservation_result": {
                        "conserved_regions": [
                            {"start": 98, "end": 209},
                            {"start": 226, "end": 315},
                        ],
                        "n_sequences": 60,
                        "alignment_length": 439,
                    }
                }
            },
        }

    monkeypatch.setattr(step, "_drive_rhea_conservation", _ok)
    out = asyncio.run(step.process({"query": "chikv", "taxon_id": 37124, "protein": "E1"}))
    rc = out["rhea_conservation"]
    assert rc["n_sequences"] == 60
    assert rc["alignment_length"] == 439
    assert len(rc["conserved_regions"]) == 2
    assert rc["aligner"] == "muscle"
    assert out["rhea_conservation_note"] is None  # no warning on success


def test_envelope_unwrap(tmp_path):
    out = asyncio.run(
        _stage(tmp_path).process({"rhea_genomic_input": {"query": "x", "taxon_id": 37124}})
    )
    assert "rhea_conservation_note" in out  # processed the unwrapped bundle (degraded: no protein)


def test_bad_input_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process(["not", "a", "dict"]))


# --------------------------------------------------------- locus-aware workload cap


@pytest.fixture
def _restore_locus():
    """Save/restore the process-wide active locus so a test's set_active_locus doesn't leak."""
    from apecx_integration.composition.runtime.execution_locus import (
        get_active_locus,
        set_active_locus,
    )

    prev = get_active_locus()
    yield set_active_locus
    set_active_locus(prev)


def test_effective_max_sequences_desktop_matches_mafft(tmp_path, _restore_locus):
    """DESKTOP/MCP locus reduces RHEA to the SAME subset the local MAFFT leg uses (25)."""
    from apecx_integration.composition.runtime.execution_locus import ExecutionLocus
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        DEFAULT_MAX_SEQUENCES,
    )

    _restore_locus(ExecutionLocus.DESKTOP)
    step = _stage(tmp_path)
    assert step._effective_max_sequences() == DEFAULT_MAX_SEQUENCES == 25


def test_effective_max_sequences_agent_keeps_large_subset(tmp_path, _restore_locus):
    """AGENT/HPC locus keeps the larger Parsl-distributed subset (60)."""
    from apecx_integration.composition.runtime.execution_locus import ExecutionLocus
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        RHEA_AGENT_MAX_SEQUENCES,
    )

    _restore_locus(ExecutionLocus.AGENT)
    step = _stage(tmp_path)
    assert step._effective_max_sequences() == RHEA_AGENT_MAX_SEQUENCES == 60


def test_effective_max_sequences_explicit_override_wins(tmp_path, _restore_locus):
    """An explicit config value pins the subset regardless of locus."""
    from apecx_integration.composition.runtime.execution_locus import ExecutionLocus

    _restore_locus(ExecutionLocus.AGENT)  # would be 60 without the override
    step = _stage(tmp_path, max_sequences=12)
    assert step._effective_max_sequences() == 12


def test_drive_rhea_passes_effective_cap_to_builder(tmp_path, monkeypatch, _restore_locus):
    """The effective cap is threaded into the inner RHEA-MUSCLE builder (not the hardcoded 60)."""
    from apecx_integration.composition.runtime.execution_locus import ExecutionLocus

    _restore_locus(ExecutionLocus.DESKTOP)
    step = _stage(tmp_path)

    captured = {}

    class _FakeWF:
        async def initialize(self):
            return None

        async def run(self, *a, **k):
            return {"workflow_output": {"n_sequences": 25, "conserved_regions": []}}

    def _fake_builder(max_sequences=None):
        captured["max_sequences"] = max_sequences
        return _FakeWF()

    monkeypatch.setattr(
        "apecx_integration.composition.workflows.viral_conserved_sites.builder"
        ".build_viral_conserved_sites_rhea_core_workflow",
        _fake_builder,
    )
    asyncio.run(step._drive_rhea_conservation(37124, "E1"))
    assert captured["max_sequences"] == 25  # desktop → MAFFT-matching reduced workload
