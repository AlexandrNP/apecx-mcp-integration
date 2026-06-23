"""Project A Step 1c: the contract checker FIRES on a REAL workflow load + warns on a
typed mismatch — and is NON-BINDING (the workflow still loads + runs).

Closes the Step-1b silent-dead-wiring gap: the unit test calls _check_link_contracts
directly, and the multidir e2e only exercises the skip (no-contract) path — neither proves
the call site in _integrate_steps_and_links actually fires + emits on a real typed-mismatch
load. Here two real EnvelopeSteps are linked with INCOMPATIBLE declared contracts; loading
the workflow must log the "DATA CONTRACT MISMATCH" warning (captured via caplog — the
nanobrain.core.workflow logger is a stdlib logger that propagates). Real load+run, no mocks.
"""

from __future__ import annotations

import asyncio
import logging

_ENVELOPE = "apecx_integration.composition.steps.envelope_step.EnvelopeStep"
_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"


def _wrapper(step_name: str, in_du: str, *, in_contract=None, out_contract=None) -> str:
    def _c(spec):
        return f"\n    contract: {spec}" if spec else ""

    return (
        f'class: "{_ENVELOPE}"\n'
        f"name: {step_name}\n"
        f"markdown_input_key: markdown\n"
        f"input_data_units:\n"
        f"  {in_du}:\n"
        f'    class: "{_DU}"\n'
        f"    name: {in_du}\n"
        f"    persistent: false{_c(in_contract)}\n"
        f"output_data_units:\n"
        f"  workflow_result:\n"
        f'    class: "{_DU}"\n'
        f"    name: workflow_result\n"
        f"    persistent: false{_c(out_contract)}\n"
        f"triggers:\n"
        f'  - class: "{_TRIGGER}"\n'
        f'    data_unit: "{in_du}"\n'
    )


_WORKFLOW = f"""\
name: contract_warn_test_workflow
config_version: 2
input_data_units:
  workflow_input: {{class: "{_DU}", name: workflow_input, persistent: false}}
output_data_units:
  workflow_output: {{class: "{_DU}", name: workflow_output, persistent: false}}
steps:
  step_a: {{class: "{_ENVELOPE}", config: "envelope_a.yml"}}
  step_b: {{class: "{_ENVELOPE}", config: "envelope_b.yml"}}
links:
  in_to_a:
    class: "nanobrain.core.link.DirectLink"
    config: {{link_type: "direct", source: "workflow_input", target: "step_a.in_a", auto_transfer: true}}
  a_to_b:
    class: "nanobrain.core.link.DirectLink"
    config: {{link_type: "direct", source: "step_a.workflow_result", target: "step_b.in_b", auto_transfer: true}}
  b_to_out:
    class: "nanobrain.core.link.DirectLink"
    config: {{link_type: "direct", source: "step_b.workflow_result", target: "workflow_output", auto_transfer: true}}
"""


def _build(tmp_path, *, out_a_contract, in_b_contract):
    # step_a's OUTPUT (workflow_result) is the producer on link a_to_b; step_b's INPUT (in_b)
    # is the consumer. Co-located (one dir) -> resolve via base_path, no search paths.
    (tmp_path / "envelope_a.yml").write_text(
        _wrapper("step_a", "in_a", out_contract=out_a_contract), encoding="utf-8"
    )
    (tmp_path / "envelope_b.yml").write_text(
        _wrapper("step_b", "in_b", in_contract=in_b_contract), encoding="utf-8"
    )
    wf = tmp_path / "wf.yml"
    wf.write_text(_WORKFLOW, encoding="utf-8")
    return wf


def test_incompatible_contracts_warn_at_load_but_run(tmp_path, caplog):
    from nanobrain.core.workflow import Workflow

    # producer workflow_result: record{markdown}; consumer in_b: file -> KIND MISMATCH.
    wf = _build(
        tmp_path,
        out_a_contract="{kind: record, required_keys: [markdown]}",
        in_b_contract="{kind: file}",
    )
    with caplog.at_level(logging.WARNING, logger="nanobrain.core.workflow"):
        workflow = Workflow.from_config(str(wf))
    assert workflow is not None  # NON-BINDING: load succeeds despite the mismatch
    assert "DATA CONTRACT MISMATCH" in caplog.text  # the call site fired + warned
    # Non-binding at runtime too: it still runs end-to-end.
    result = asyncio.run(workflow.run({"workflow_input": {"markdown": "hi"}}, timeout=30))
    assert "hi" in __import__("json").dumps(result, default=str)


def test_compatible_contracts_no_warn(tmp_path, caplog):
    from nanobrain.core.workflow import Workflow

    # producer record{markdown}; consumer record{markdown} -> compatible -> no warning.
    wf = _build(
        tmp_path,
        out_a_contract="{kind: record, required_keys: [markdown]}",
        in_b_contract="{kind: record, required_keys: [markdown]}",
    )
    with caplog.at_level(logging.WARNING, logger="nanobrain.core.workflow"):
        workflow = Workflow.from_config(str(wf))
    assert workflow is not None
    assert "DATA CONTRACT MISMATCH" not in caplog.text
