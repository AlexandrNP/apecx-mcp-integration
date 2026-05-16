"""rhea_workflow codegen — the code generator uses Rhea as an MCP server.

The user's design directive: *"Rhea should be utilized by the code
generator as an MCP server."* This module is that code generator.

What it does, per Open-Rosalind problem:

1. **Discover** — on first call, connect to the Rhea MCP server
   (``$RHEA_MCP_URL``) via ``RheaMCPDiscovery`` and pull the tool
   catalog as UTD dicts. Also register ``RheaAdapter`` with
   ``ToolBackendRegistry`` so the generated workflow can dispatch at
   run time.
2. **Generate** — pick the Rhea tool whose name best matches the
   problem's expected skill, then GENERATE a nanobrain workflow
   (programmatically, via the lightweight ``WorkflowBuilder``) that
   wires a ``ToolExecutionStep`` for that tool. This is the
   "workflow generation" step — the codegen emits a real workflow,
   not a code string.
3. **Run** — execute the generated workflow against the problem's
   input, drain the cascade, and return the tool's result rendered
   as a string (so the benchmark harness's keyword-presence
   ``test_code`` can score it).

Honest scope + gating
---------------------

* **GATED on ``$RHEA_MCP_URL``.** No env var → the factory FAILS
  LOUDLY. A codegen that silently produced empty answers because
  Rhea was unreachable is exactly the silent-failure shape the
  workspace policy forbids.
* **GATED on a Rhea worker actually hosting Open-Rosalind's tools.**
  Registering OR's 30 bio tool modules with a Rhea worker is a
  ``rhea/``-side task (out of apecx-mcp-integration's writable
  scope). When ``$RHEA_MCP_URL`` points at a Rhea worker that does
  NOT host the OR tools, discovery succeeds but tool-matching fails
  loudly — never a silent wrong-tool dispatch.
* The "generated workflow" is built with the lightweight
  ``WorkflowBuilder`` (one of the three legitimate construction
  paths). The hand-authored YAML at
  ``composition/workflows/open_rosalind_rhea/workflow.yml`` is the
  canonical reference shape this codegen reproduces per-problem.

See docs/open_rosalind_rhea_standalone_case.md for the full design +
blocker analysis.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable

from tests.benchmarks.types import BenchmarkProblem


def make_rhea_workflow_codegen() -> Callable[[BenchmarkProblem], str]:
    """Build the rhea_workflow codegen callable.

    FAIL-FAST at factory time when ``$RHEA_MCP_URL`` is unset — the
    codegen cannot function without a Rhea MCP server, and discovering
    that at factory time (not per-problem) gives a single clear error
    instead of N identical per-problem failures.
    """
    from nanobrain.library.tools.rhea_adapter import RheaAdapter
    from nanobrain.library.tools.rhea_discovery import RheaMCPDiscovery

    # FAIL-FAST: both calls raise ComponentConfigurationError when
    # $RHEA_MCP_URL is unset (see their from_env docstrings).
    discovery = RheaMCPDiscovery.from_env()
    RheaAdapter.from_env(register=True)  # registers the 'rhea' backend

    # Discover the Rhea tool catalog once, at factory time.
    tool_utds: list[dict] = asyncio.run(discovery.discover())
    asyncio.run(discovery.aclose())
    # Index by the MCP-side tool name for matching.
    catalog: dict[str, dict] = {}
    for utd in tool_utds:
        rhea_name = utd.get("provenance_pin", {}).get("mcp_support", {}).get("rhea_tool_name", "")
        if rhea_name:
            catalog[rhea_name] = utd

    def _codegen(problem: BenchmarkProblem) -> str:
        return asyncio.run(_run_async(problem))

    async def _run_async(problem: BenchmarkProblem) -> str:

        # 1. Pick the Rhea tool for this problem. Open-Rosalind problems
        #    carry an 'expected_skill' in metadata; map skill -> tool.
        #    For the sequence_basic subset that is sequence.analyze.
        expected_skill = problem.metadata.get("expected_skill", "")
        tool_name = _match_tool(expected_skill, catalog)
        if tool_name is None:
            # FAIL LOUD — wrong-tool dispatch would produce a confidently
            # wrong answer. Better a clear error.
            raise RuntimeError(
                f"rhea_workflow: no Rhea tool matches expected_skill "
                f"{expected_skill!r} for problem {problem.problem_id}. "
                f"Rhea catalog: {sorted(catalog)}. The Rhea worker at "
                f"$RHEA_MCP_URL may not host Open-Rosalind's tools — see "
                f"docs/open_rosalind_rhea_standalone_case.md."
            )
        utd = catalog[tool_name]

        # 2. GENERATE the workflow (lightweight WorkflowBuilder path).
        wf = _generate_workflow(utd)

        # 3. RUN it. The input is the tool's input dict — for
        #    sequence.analyze that is {"sequence": <the OR input>}.
        tool_input = _problem_to_tool_input(problem, utd)
        await wf.process({"sequence_tool_input": tool_input})
        drained = await wf.wait_for_cascade(timeout=120.0, settle_ms=100)
        if not drained:
            raise RuntimeError(
                f"rhea_workflow: cascade did not drain for "
                f"{problem.problem_id} — Rhea worker timeout or hang."
            )
        step = wf.child_steps["sequence_tool"]
        result = await step.step_output_data_units["sequence_tool_output"].get()
        return _render_result(result)

    return _codegen


def _match_tool(expected_skill: str, catalog: dict[str, dict]) -> str | None:
    """Map an Open-Rosalind ``expected_skill`` to a Rhea tool name.

    Conservative substring match: the skill ``sequence_basic_analysis``
    matches a tool named ``sequence.analyze`` (shared 'sequence' stem).
    Returns None when nothing matches — the caller FAILS LOUD rather
    than dispatching a wrong tool.
    """
    skill_lower = expected_skill.lower()
    # Exact-ish: skill stem in tool name OR tool stem in skill.
    for tool_name in catalog:
        tn = tool_name.lower()
        tn_stem = tn.split(".")[0]
        if tn_stem and (tn_stem in skill_lower or skill_lower.split("_")[0] in tn):
            return tool_name
    return None


def _generate_workflow(utd: dict):
    """Generate a nanobrain workflow wiring a ToolExecutionStep for ``utd``.

    Uses the lightweight WorkflowBuilder — the workflow is GENERATED
    programmatically per-problem, with the discovered UTD inlined into
    the step config via a tempfile (ToolExecutionStep needs a file-
    backed step config; the UTD is inlined into it).
    """
    import os
    import tempfile

    import yaml
    from nanobrain.lightweight import WorkflowBuilder

    # Materialize a step config YAML with the discovered UTD inline.
    # ToolExecutionStep self-unwraps the {sequence_tool_input: {...}}
    # trigger envelope (the input-DU name is not a declared UTD input),
    # so it can be driven through a workflow cascade directly — no
    # apecx-side subclass needed.
    step_config = {
        "class": "nanobrain.library.steps.tool_execution_step.ToolExecutionStep",
        "name": "sequence_tool",
        "description": "Generated ToolExecutionStep dispatching to Rhea.",
        "tool_descriptor": utd,
        "input_data_units": {
            "sequence_tool_input": {
                "class": "nanobrain.core.data_unit.DataUnitMemory",
                "name": "sequence_tool_input",
                "persistent": False,
            }
        },
        "output_data_units": {
            "sequence_tool_output": {
                "class": "nanobrain.core.data_unit.DataUnitMemory",
                "name": "sequence_tool_output",
                "persistent": False,
            }
        },
        "triggers": [
            {
                "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                "data_unit": "sequence_tool_input",
            }
        ],
    }
    fd, step_path = tempfile.mkstemp(suffix=".yml", prefix="rhea_step_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(step_config, fh, sort_keys=False)

        builder = WorkflowBuilder(
            name="rhea_workflow_generated",
            description="Per-problem generated OR-via-Rhea workflow.",
        )
        builder.add_input("workflow_input", "DataUnitMemory")
        builder.add_output("workflow_output", "DataUnitMemory")
        builder.add_step(
            "sequence_tool",
            "nanobrain.library.steps.tool_execution_step.ToolExecutionStep",
            config=step_path,
        )
        builder.add_link(
            source="workflow_input",
            target="sequence_tool.sequence_tool_input",
            link_type="direct",
            link_name="input_to_tool",
            auto_transfer=True,
        )
        builder.add_link(
            source="sequence_tool.sequence_tool_output",
            target="workflow_output",
            link_type="direct",
            link_name="tool_to_output",
            auto_transfer=True,
        )
        _nest_link_configs(builder.workflow_config)
        return builder.load()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(step_path)


def _nest_link_configs(wf_config: dict) -> None:
    """Repair WorkflowBuilder's flat-link shape — see the lightweight
    builder modules for the full failure-shape note."""
    links = wf_config.get("links")
    if not isinstance(links, dict):
        return
    for link_name, entry in list(links.items()):
        if not isinstance(entry, dict) or ("config" in entry and isinstance(entry["config"], dict)):
            continue
        nested = {k: v for k, v in entry.items() if k not in {"name", "class", "description"}}
        nested.setdefault("link_type", "direct")
        wf_config["links"][link_name] = {
            "name": entry.get("name", link_name),
            "class": entry.get("class"),
            "description": entry.get("description"),
            "config": nested,
        }


def _problem_to_tool_input(problem: BenchmarkProblem, utd: dict) -> dict:
    """Build the tool's input dict from the OR problem.

    For sequence.analyze the single required input is ``sequence``;
    Open-Rosalind problems carry the raw input in
    ``metadata['source_input']``.
    """
    source_input = problem.metadata.get("source_input", "")
    input_names = [i.get("name") for i in utd.get("inputs", []) if i.get("name")]
    if input_names == ["sequence"]:
        return {"sequence": source_input}
    # Generic fallback: first required input gets the raw source.
    for spec in utd.get("inputs", []):
        if spec.get("required"):
            return {spec["name"]: source_input}
    return {}


def _render_result(result) -> str:
    """Render the Rhea tool's result as a string for keyword scoring."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # Flatten dict values into a readable string so the OR
        # keyword-presence test_code can find facts in it.
        return json.dumps(result, default=str)
    return str(result)


__all__ = ["make_rhea_workflow_codegen"]
