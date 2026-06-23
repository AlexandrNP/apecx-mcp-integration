"""Project A Step 2: BINDING contract enforcement on a REAL workflow run.

At config_version:3 a producer emitting a value that VIOLATES its declared output contract
makes the runtime DataUnitMemory.set() guard RAISE. The violation fires in the link/trigger
transfer (the set() on the output DU), NOT inside process() — so it surfaces as a trigger
bound-action ERROR (logged ContractViolationError), NOT a G37 step_failed event. Workflow.run
swallows it (G127) and the value never flows downstream. So we assert on the BLOCKED OUTPUT
VALUE + the logged ContractViolationError, NEVER on status. The v3-compatible control proves
the same shape delivers the value end-to-end (so the blocked output is attributable to the
guard). At config_version:2 the same violating contract runs to completion (non-binding warn).
Real EnvelopeStep + real Workflow.run, no mocks.
"""

from __future__ import annotations

import asyncio
import json
import logging

_ENVELOPE = "apecx_integration.composition.steps.envelope_step.EnvelopeStep"
_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"


def _wrapper(step_name: str, in_du: str, out_contract: str | None = None) -> str:
    c = f"\n    contract: {out_contract}" if out_contract else ""
    return (
        f'class: "{_ENVELOPE}"\n'
        f"name: {step_name}\n"
        f"markdown_input_key: markdown\n"
        f"input_data_units:\n"
        f'  {in_du}: {{class: "{_DU}", name: {in_du}, persistent: false}}\n'
        f"output_data_units:\n"
        f"  workflow_result:\n"
        f'    class: "{_DU}"\n'
        f"    name: workflow_result\n"
        f"    persistent: false{c}\n"
        f"triggers:\n"
        f'  - class: "{_TRIGGER}"\n'
        f'    data_unit: "{in_du}"\n'
    )


def _workflow(config_version: int) -> str:
    return f"""\
name: contract_enforcement_test
config_version: {config_version}
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


def _build(tmp_path, *, config_version: int, out_a_contract: str | None):
    # step_a's output (workflow_result) carries the contract under test; in_b is UNDECLARED
    # so the LOAD checker skips (gradual) and we isolate the RUNTIME set() guard.
    (tmp_path / "envelope_a.yml").write_text(
        _wrapper("step_a", "in_a", out_a_contract), encoding="utf-8"
    )
    (tmp_path / "envelope_b.yml").write_text(_wrapper("step_b", "in_b"), encoding="utf-8")
    wf = tmp_path / "wf.yml"
    wf.write_text(_workflow(config_version), encoding="utf-8")
    return wf


def test_v3_runtime_violation_blocks_flow(tmp_path, caplog):
    from nanobrain.core.workflow import Workflow

    # EnvelopeStep emits a dict; declaring workflow_result as a `collection` makes the actual
    # value violate the contract -> the set() guard RAISES under v3. Workflow.run swallows the
    # raise (G127) -> the value never flows downstream. Assert on the VALUE (blocked) + the
    # logged ContractViolationError (the guard fired in the link/trigger transfer, not in
    # process(), so it surfaces as a bound-action error, not a G37 step_failed event).
    wf = _build(tmp_path, config_version=3, out_a_contract="{kind: collection}")
    workflow = Workflow.from_config(str(wf))
    with caplog.at_level(logging.ERROR):
        result = asyncio.run(workflow.run({"workflow_input": {"markdown": "hi"}}, timeout=30))
    text = caplog.text.lower()
    assert "contractviolationerror" in text or "contract violation" in text
    assert "hi" not in json.dumps(result.get("workflow_output"), default=str)


def test_v3_compatible_runs_clean(tmp_path):
    from nanobrain.core.workflow import Workflow

    # workflow_result as a `record` (no required keys) — the emitted dict satisfies it.
    wf = _build(tmp_path, config_version=3, out_a_contract="{kind: record}")
    workflow = Workflow.from_config(str(wf))
    result = asyncio.run(workflow.run({"workflow_input": {"markdown": "hi"}}, timeout=30))
    assert "hi" in json.dumps(result, default=str)  # ran end-to-end, no violation


def test_v2_violation_is_non_binding(tmp_path, caplog):
    from nanobrain.core.workflow import Workflow

    # Same violating contract, but config_version:2 -> WARN, not raise; the workflow runs.
    wf = _build(tmp_path, config_version=2, out_a_contract="{kind: collection}")
    workflow = Workflow.from_config(str(wf))
    with caplog.at_level(logging.WARNING, logger="nanobrain.core.data_unit"):
        result = asyncio.run(workflow.run({"workflow_input": {"markdown": "hi"}}, timeout=30))
    assert "contract violation" in caplog.text.lower()
    assert "hi" in json.dumps(result, default=str)  # non-binding: ran to completion
