"""AC-NATIVE — nanobrain-compliance guards for viral_epitope_evidence_review.

These assert the no-silent-failure properties at the level a regression would
break them: the generated workflow config (which becomes the per-step YAML the
lightweight builder loads via Workflow.from_config).

- config_version == 2  ⇒ the framework's v2 model_validator injects
  ``auto_transfer: true`` on every DirectLink at load (G7 Step 5). Verified at
  load time by the framework's own debug log; here we guard the PRECONDITION
  (v2 + no link opting OUT via auto_transfer:false) deterministically, no live
  cascade needed.
- The lightweight ``build_...()`` path loads through Workflow.from_config (the
  builder materializes per-step YAMLs + a workflow YAML), so the load test below
  also exercises the YAML construction path — not a second, untested route.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.workflows.viral_epitope_evidence_review.builder import (
    _evidence_workflow_builder,
    build_viral_epitope_evidence_review_workflow,
)


@pytest.fixture(autouse=True)
def _disable_globus_search(monkeypatch):
    """Keep the workflow build offline without leaking process env.

    Previously a module-level ``os.environ.setdefault`` set this flag and
    never restored it, polluting every later test in the session (notably
    test_globus_search.py, whose CapturingClient assertions short-circuited
    because search() saw the leaked APECX_GLOBUS_SEARCH_DISABLED=1). Scoping
    it to a monkeypatch fixture restores the env after each test.
    """
    monkeypatch.setenv("APECX_GLOBUS_SEARCH_DISABLED", "1")


def test_generated_config_is_v2_with_no_auto_transfer_optout():
    cfg = _evidence_workflow_builder().get_config()
    assert cfg["config_version"] == 2, "config_version must be 2 (auto_transfer default-flip)"

    links = cfg["links"]
    assert len(links) == 14, (
        f"expected 14 links (both fan-ins + data-readiness + structural-reasoning + "
        f"functional-validation edges), got {len(links)}"
    )
    for name, entry in links.items():
        link_cfg = entry["config"]
        assert link_cfg["link_type"] == "direct", f"{name}: only DirectLinks expected"
        # The dominant silent failure is auto_transfer:false. v2 injects true at load;
        # what we must guard is that NO link opts OUT explicitly.
        assert link_cfg.get("auto_transfer", True) is not False, (
            f"{name}: declares auto_transfer:false — the dominant nanobrain silent failure"
        )


def test_lightweight_load_builds_expected_dag_via_from_config():
    # build_...() runs builder.load(), which writes per-step YAMLs + a workflow YAML and
    # calls Workflow.from_config — so this exercises the YAML construction path too.
    wf = build_viral_epitope_evidence_review_workflow()
    children = getattr(wf, "child_steps", None) or getattr(wf, "_child_steps", None)
    assert isinstance(children, dict)
    assert set(children) == {
        "normalize",
        "assemble",
        "data_readiness",
        "structural",
        "sequence",
        "merge",
        "reasoning",
        "functional",
        "review",
        "gate",
        "envelope",
    }


def test_sequence_and_merge_fanin_wired():
    """E2-C1: the sequence-conservation leg + the merge fan-in are present, the merge joins
    the structural bundle + the sequence result, and review reads the MERGED bundle (not the
    raw structural bundle). Guards a regression that would drop the sequence stage."""
    cfg = _evidence_workflow_builder().get_config()
    steps = cfg["steps"]
    assert "sequence" in steps and "merge" in steps

    # The sequence step nests the conserved-sites builder; its own input DU (G117) differs
    # from the inner workflow's first-step input DU (fetch_in).
    seq = steps["sequence"]
    assert seq["class"].endswith("SequenceConservationSubworkflowStep")
    assert "sequence_params" in seq["input_data_units"]
    assert "fetch_in" not in seq["input_data_units"]

    # merge fan-in: an AllDataReceivedTrigger over both legs.
    merge = steps["merge"]
    trig = merge["triggers"][0]
    assert trig["class"].endswith("AllDataReceivedTrigger")
    assert set(trig["data_units"]) == {"structural_in", "sequence_in"}

    link_pairs = {(e["config"]["source"], e["config"]["target"]) for e in cfg["links"].values()}
    assert ("normalize.normalize_out", "sequence.sequence_params") in link_pairs
    assert ("structural.structural_bundle", "merge.structural_in") in link_pairs
    assert ("sequence.sequence_result", "merge.sequence_in") in link_pairs
    # E2-P + C3: merge feeds structural-reasoning, which feeds functional-validation,
    # which feeds review.
    assert ("merge.merged_bundle", "reasoning.reasoning_input") in link_pairs
    assert ("reasoning.reasoning_output", "functional.functional_input") in link_pairs
    assert ("functional.functional_output", "review.review_input") in link_pairs
    # The OLD direct merge→review edge must be gone (review now reads the reasoning bundle).
    assert ("merge.merged_bundle", "review.review_input") not in link_pairs
    # The OLD direct reasoning→review edge must be gone (functional sits between them now).
    assert ("reasoning.reasoning_output", "review.review_input") not in link_pairs
    # The OLD direct structural→review edge must be gone (review now reads the merged bundle).
    assert ("structural.structural_bundle", "review.review_input") not in link_pairs
    # The design-gate fan-in (#2) is untouched.
    assert ("review.review_output", "gate.review_in") in link_pairs
    assert ("normalize.normalize_out", "gate.control_in") in link_pairs


def test_entry_step_input_schema_requires_query():
    """RoC-2c source: the ENTRY step (normalize) step_input_schema drives find_param_gaps.
    Its input DU (normalize_input) is the deposit point = catalog input_envelope_key."""
    cfg = _evidence_workflow_builder().get_config()
    normalize = cfg["steps"]["normalize"]
    schema = normalize["step_input_schema"]["json_schema"]
    assert schema["required"] == ["normalize_input"]
    assert schema["properties"]["normalize_input"]["required"] == ["query"]


def test_control_fanned_from_normalize_not_workflow_input():
    """REGRESSION: gate.control_in must be fed from normalize.normalize_out (a DU the
    deposit actually sets), NOT from workflow_input (which run_workflow never sets —
    the deposit goes to input_envelope_key=normalize_input). This guards the silent
    failure where the gate never fires because control_in stays empty."""
    cfg = _evidence_workflow_builder().get_config()
    sources = {e["config"]["source"]: e["config"]["target"] for e in cfg["links"].values()}
    # control_in is fed by normalize.normalize_out, and the same DU also feeds assemble.
    assert "normalize.normalize_out" in sources or any(
        e["config"]["target"] == "gate.control_in"
        and e["config"]["source"] == "normalize.normalize_out"
        for e in cfg["links"].values()
    )
    targets_of_normalize_out = {
        e["config"]["target"]
        for e in cfg["links"].values()
        if e["config"]["source"] == "normalize.normalize_out"
    }
    assert "gate.control_in" in targets_of_normalize_out
    assert "assemble.assembly_input" in targets_of_normalize_out
    # And NOTHING fans control from workflow_input into the gate (the old bug).
    assert not any(
        e["config"]["source"] == "workflow_input" and e["config"]["target"] == "gate.control_in"
        for e in cfg["links"].values()
    )
