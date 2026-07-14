"""Pure-logic unit tests for the epitope eval (no infra). The live run is in
test_epitope_e2e (Ollama/MAFFT-gated)."""

from __future__ import annotations

import json

from tests.eval.epitope_checks import (
    check_completeness,
    check_full_artifacts,
    check_report_references,
    check_streaming,
    check_structural_reasoning_produced,
    protabank_count,
)
from tests.eval.epitope_eval_loop import (
    _parse_question,
    diagnose,
    protabank_verdict,
    should_terminate,
    split_held_out,
)
from tests.eval.run_epitope import EpitopeResult


def _write_struct(tmp_path, *, records, regions, structural_reasoning):
    """Materialize a tool_outputs/ dir with the three JSONs the structural check reads."""
    to = tmp_path / "tool_outputs"
    to.mkdir(exist_ok=True)
    (to / "structural_records.json").write_text(json.dumps(records))
    (to / "conserved_regions.json").write_text(json.dumps(regions))
    (to / "structural_reasoning.json").write_text(json.dumps(structural_reasoning))
    return tmp_path


def test_structural_check_passes_when_real_sasa_produced(tmp_path):
    # the FIXED chikungunya E1 state: valid PDB + regions + real SASA.
    d = _write_struct(
        tmp_path,
        records=[{"pdb": "2XFB"}] * 10,
        regions=[{"start": 1}] * 5,
        structural_reasoning={"available": True, "n_exposed": 96, "pdb_id": "2XFB", "note": None},
    )
    assert check_structural_reasoning_produced(d, expect_structure=True).passed


def test_structural_check_fails_on_valid_pdb_but_no_sasa(tmp_path):
    # the DF1/DF2 state: valid PDB present, but structural analysis produced nothing (available False).
    # A proceed_note explaining WHY does NOT excuse a structure-expected entity.
    d = _write_struct(
        tmp_path,
        records=[{"pdb": "2XFB"}] * 10,
        regions=[],
        structural_reasoning={
            "available": False,
            "n_exposed": None,
            "pdb_id": "2XFB",
            "note": "No conserved regions were available to map onto structure 2XFB; skipped.",
        },
    )
    r = check_structural_reasoning_produced(d, expect_structure=True)
    assert not r.passed
    assert "2XFB" in r.evidence  # the diagnosis surfaces the PDB + reason


def test_structural_check_fails_on_pymol_infra_miss(tmp_path):
    d = _write_struct(
        tmp_path,
        records=[{"pdb": "6M0J"}] * 8,
        regions=[{"start": 3}] * 4,
        structural_reasoning={
            "available": False,
            "n_exposed": None,
            "pdb_id": "6M0J",
            "note": "Containerized PyMOL structural analysis is unavailable: ...",
        },
    )
    assert not check_structural_reasoning_produced(d, expect_structure=True).passed


def test_structural_check_na_when_no_structure_expected(tmp_path):
    d = _write_struct(
        tmp_path,
        records=[],
        regions=[],
        structural_reasoning={"available": False, "note": "No loadable PDB structure."},
    )
    assert check_structural_reasoning_produced(d, expect_structure=False).passed


def test_structural_check_is_wired_into_the_live_driver():
    """Regression: check_structural_reasoning_produced was implemented + unit-tested but NEVER added to
    run_epitope's live check list, so every 'valid PDB but zero SASA' run passed silently. Pin the wiring
    (the check is invoked in the live driver AND the loop knows how to categorize its failure)."""
    import inspect

    from tests.eval import run_epitope as driver
    from tests.eval.epitope_eval_loop import _CHECK_TO_CATEGORY

    assert "check_structural_reasoning_produced(" in inspect.getsource(driver.run_epitope)
    assert "structural_reasoning_produced" in _CHECK_TO_CATEGORY


def _events(steps, complete=True):
    et = {"step_start", "step_complete"} if complete else {"step_start"}
    return {s: set(et) for s in steps}


def test_streaming_and_completeness():
    steps = {"normalize", "resolve", "review"}
    assert check_streaming(steps, _events(steps)).passed
    assert not check_streaming(steps, _events({"normalize"})).passed  # others silent
    assert check_completeness(steps, _events(steps)).passed
    assert not check_completeness(steps, _events(steps, complete=False)).passed  # never completed
    # a NESTED step failure (not an expected top-level step) is caught/degraded → does NOT fail (EF7)
    ev = _events(steps)
    ev["muscle_alignment"] = {"step_start", "step_failed"}  # nested, not in expected
    assert check_completeness(steps, ev).passed
    # but a TOP-LEVEL (expected) step failure DOES fail completeness
    ev2 = _events(steps)
    ev2["review"] = {"step_start", "step_failed"}
    assert not check_completeness(steps, ev2).passed


def test_full_artifacts_reason_aware(tmp_path):
    run = tmp_path / "run"
    (run / "tool_outputs").mkdir(parents=True)
    (run / "report.md").write_text("x" * 50)
    (run / "data.json").write_text(json.dumps({"kind": "bundle", "parts": {}}))
    (run / "tool_outputs" / "structural_records.json").write_text(json.dumps([{"id": "6m0j"}]))
    (run / "tool_outputs" / "publications.json").write_text(json.dumps([{"doi": "10.x"}]))
    # conserved_regions EMPTY but a sequence-conservation proceed-note → OK (honest degrade)
    (run / "tool_outputs" / "conserved_regions.json").write_text("[]")
    ok = check_full_artifacts(run, {"sequence conservation"})
    assert ok.passed, ok.evidence
    # same EMPTY conserved_regions with NO proceed-note → SILENT failure → FAIL
    bad = check_full_artifacts(run, set())
    assert not bad.passed and "silent failure" in bad.evidence.lower()
    # an ALWAYS-content artifact empty → FAIL regardless of degrade notes
    (run / "tool_outputs" / "structural_records.json").write_text("[]")
    assert not check_full_artifacts(run, {"sequence conservation"}).passed


def test_report_references():
    good = (
        "# Answer\n## Data actually used\n## Cross-referenced epitope candidates\n"
        "## Structural evidence (PDB / EMDB)\n## Evidence coverage\n## Sources and evidence\n"
        "- **[BV-BRC genome 11036.7]** Chikungunya — titled\n"
    )
    assert check_report_references(good).passed
    assert not check_report_references(
        "# Answer only, no sections"
    ).passed  # missing sections + cites


def test_protabank_count_parsing():
    assert protabank_count("- **protabank**: 5 available / 2 used") == 5
    assert protabank_count("- **protabank**: 0 available / 0 used _(searched, no records)_") == 0
    assert protabank_count("no mention here") is None  # silently omitted


def test_protabank_verdict_sample_aware():
    # all-0 AND none excluded → gated (genuinely never surfaces data)
    runs = [
        EpitopeResult("a", protabank=0, checks=[], status="ok"),
        EpitopeResult("b", protabank=0, checks=[], status="ok"),
    ]
    v = protabank_verdict(runs)
    assert v is not None and v.category == "protabank_never_retrieved" and v.gate == "gated"
    # any virus surfaced records → no verdict (ProtaBank works)
    assert protabank_verdict([runs[0], EpitopeResult("b", protabank=3)]) is None
    # all-0 BUT a run excluded (protabank=None, e.g. the data-rich heavy virus halted) → informational,
    # NOT a reliable 'never retrieved' (lesson EF1: EF2 masked the data-rich viruses)
    biased = protabank_verdict([runs[0], EpitopeResult("heavy-virus", protabank=None)])
    assert biased is not None
    assert biased.category == "protabank_zero_but_biased_sample" and biased.gate == "informational"


def test_rhea_unavailable_is_informational():
    # the protein/sequence leg requires RHEA (fail-closed); RHEA down → environment, not a gated bug
    r = EpitopeResult(
        "chikungunya virus|E1",
        status="error",
        error="rhea_unavailable: align: rhea subworkflow produced no 'workflow_output'",
    )
    items = diagnose([r])
    assert len(items) == 1
    assert items[0].category == "rhea_unavailable" and items[0].gate == "informational"


def test_diagnose_and_parse_and_terminate():
    from tests.eval.epitope_checks import CheckResult

    good = EpitopeResult("ok", checks=[CheckResult("streaming", True, "")], status="ok")
    # .passed needs all checks pass + no error; give a fully-passing run
    good.checks = [
        CheckResult(n, True, "")
        for n in (
            "streaming",
            "completeness",
            "full_artifacts",
            "report_references",
            "protabank_reported",
        )
    ]
    flake = EpitopeResult("flake", error="connection reset", transient=True)
    items = diagnose([good, flake])
    by_q = {f.query: f for f in items}
    assert "ok" not in by_q and by_q["flake"].gate == "auto_safe"

    assert _parse_question("dengue virus|envelope protein") == ("dengue virus", "envelope protein")
    assert _parse_question("chikungunya virus") == ("chikungunya virus", None)

    train, held = split_held_out(["c", "a", "b", "d"], every=2)
    assert held == ["a", "c"] and set(train).isdisjoint(held)
    assert should_terminate([{}, {}], max_iters=2) == (True, "max_iters")


def test_harmonization_disclosed_parsing():
    from tests.eval.epitope_checks import (
        check_harmonization_disclosed,
        harmonization_imprecise_indices,
    )

    md = (
        "Coverage gaps: no antiviraldb record; protabank: 1 record(s) via taxon-IMPRECISE raw "
        "free-text (taxon-harmonization broken); bvbrc_protein: 884 record(s) via taxon-IMPRECISE raw"
    )
    assert set(harmonization_imprecise_indices(md)) == {"protabank", "bvbrc_protein"}
    c = check_harmonization_disclosed(md)
    assert c.passed and "2" in c.evidence  # informational, surfaces the count
    assert harmonization_imprecise_indices("clean report, no fallback") == []
