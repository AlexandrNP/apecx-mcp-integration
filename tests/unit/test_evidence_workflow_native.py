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

import os

os.environ.setdefault("APECX_GLOBUS_SEARCH_DISABLED", "1")

from apecx_integration.composition.workflows.viral_epitope_evidence_review.builder import (
    _evidence_workflow_builder,
    build_viral_epitope_evidence_review_workflow,
)


def test_generated_config_is_v2_with_no_auto_transfer_optout():
    cfg = _evidence_workflow_builder().get_config()
    assert cfg["config_version"] == 2, "config_version must be 2 (auto_transfer default-flip)"

    links = cfg["links"]
    assert len(links) == 8, f"expected 8 links (incl. the fan-in edge), got {len(links)}"
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
    assert set(children) == {"normalize", "assemble", "structural", "review", "gate", "envelope"}


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
