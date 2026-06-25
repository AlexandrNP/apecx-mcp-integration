"""Pure-logic unit tests for the epitope eval (no infra). The live run is in
test_epitope_e2e (Ollama/MAFFT-gated)."""

from __future__ import annotations

import json

from tests.eval.epitope_checks import (
    check_completeness,
    check_full_artifacts,
    check_report_references,
    check_streaming,
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


def _events(steps, complete=True):
    et = {"step_start", "step_complete"} if complete else {"step_start"}
    return {s: set(et) for s in steps}


def test_streaming_and_completeness():
    steps = {"normalize", "resolve", "review"}
    assert check_streaming(steps, _events(steps)).passed
    assert not check_streaming(steps, _events({"normalize"})).passed  # others silent
    assert check_completeness(steps, _events(steps)).passed
    assert not check_completeness(steps, _events(steps, complete=False)).passed  # never completed


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
