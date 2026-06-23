"""T8: end-to-end proof that a workflow REUSING wrappers from MULTIPLE catalog
dirs resolves AND runs.

The original failure was at workflow LOAD ("CONFIGURATION PATH RESOLUTION FAILED:
entity_extraction.yml") when a composed workflow referenced a wrapper outside the
single staged steps/ dir. This test reproduces the shape deterministically (no LLM):
two EnvelopeStep wrappers in TWO SEPARATE catalog-root dirs, a workflow that chains
them, loaded via Workflow.from_config(config_search_paths=[rootA, rootB]) (what the
executor injects, T4) and run via Workflow.run. Success is asserted from the OUTPUT
VALUE (markdown threaded through both steps), never from `status` (G127).

EnvelopeStep is reused because it is importable from src (no test-package path issue),
deterministic (no LLM), and a terminal WorkflowResult dict carries a `markdown` key, so
a 2nd EnvelopeStep legitimately consumes the 1st's output.
"""

from __future__ import annotations

import asyncio
import json

import pytest

_ENVELOPE = "apecx_integration.composition.steps.envelope_step.EnvelopeStep"
_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"


def _wrapper(step_name: str, in_du: str) -> str:
    return (
        f'class: "{_ENVELOPE}"\n'
        f"name: {step_name}\n"
        f"markdown_input_key: markdown\n"
        f"input_data_units:\n"
        f"  {in_du}:\n"
        f'    class: "{_DU}"\n'
        f"    name: {in_du}\n"
        f"    persistent: false\n"
        f"output_data_units:\n"
        f"  workflow_result:\n"
        f'    class: "{_DU}"\n'
        f"    name: workflow_result\n"
        f"    persistent: false\n"
        f"triggers:\n"
        f'  - class: "{_TRIGGER}"\n'
        f'    data_unit: "{in_du}"\n'
    )


_WORKFLOW = f"""\
name: multidir_reuse_test_workflow
config_version: 2
input_data_units:
  workflow_input:
    class: "{_DU}"
    name: workflow_input
    persistent: false
output_data_units:
  workflow_output:
    class: "{_DU}"
    name: workflow_output
    persistent: false
steps:
  step_a:
    class: "{_ENVELOPE}"
    config: "envelope_a.yml"
  step_b:
    class: "{_ENVELOPE}"
    config: "envelope_b.yml"
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


def _contains(obj, needle: str) -> bool:
    return needle in json.dumps(obj, default=str)


def test_multidir_reuse_resolves_and_runs(tmp_path):
    # Two catalog roots, each holding ONE step wrapper. base/ has neither -> the
    # refs resolve ONLY via config_search_paths (Strategy 7).
    root_a = tmp_path / "catalog_a"
    root_b = tmp_path / "catalog_b"
    base = tmp_path / "base"
    for d in (root_a, root_b, base):
        d.mkdir()
    (root_a / "envelope_a.yml").write_text(_wrapper("step_a", "in_a"), encoding="utf-8")
    (root_b / "envelope_b.yml").write_text(_wrapper("step_b", "in_b"), encoding="utf-8")
    wf_path = base / "multidir_workflow.yml"
    wf_path.write_text(_WORKFLOW, encoding="utf-8")

    from nanobrain.core.workflow import Workflow

    # LOAD: both cross-dir step configs resolve via the injected catalog roots.
    # (This is the exact point the original bug failed: PATH RESOLUTION FAILED.)
    workflow = Workflow.from_config(str(wf_path), config_search_paths=[str(root_a), str(root_b)])
    assert workflow is not None

    # RUN: markdown threads through both reused steps. Assert on the VALUE.
    # Input keyed by the workflow-level input DU name (G122 deposit contract).
    result = asyncio.run(
        workflow.run({"workflow_input": {"markdown": "hello-multidir"}}, timeout=30)
    )
    assert _contains(result, "hello-multidir"), (
        f"output did not carry the markdown through both reused steps; got: {result}"
    )


def test_load_fails_without_search_paths(tmp_path):
    # Control: WITHOUT the catalog roots, the same workflow CANNOT resolve the
    # cross-dir wrappers -> load fails. Proves the search-path is load-bearing,
    # not incidental (the refs genuinely live outside base/).
    root_a = tmp_path / "catalog_a"
    root_b = tmp_path / "catalog_b"
    base = tmp_path / "base"
    for d in (root_a, root_b, base):
        d.mkdir()
    (root_a / "envelope_a.yml").write_text(_wrapper("step_a", "in_a"), encoding="utf-8")
    (root_b / "envelope_b.yml").write_text(_wrapper("step_b", "in_b"), encoding="utf-8")
    wf_path = base / "multidir_workflow.yml"
    wf_path.write_text(_WORKFLOW, encoding="utf-8")

    from nanobrain.core.workflow import Workflow

    with pytest.raises((ValueError, FileNotFoundError)):
        Workflow.from_config(str(wf_path))  # no config_search_paths -> cross-dir refs unresolvable
