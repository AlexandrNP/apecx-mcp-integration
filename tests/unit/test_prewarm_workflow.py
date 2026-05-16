"""Unit tests for the nanobrain pre-warm workflow.

Scope:

* Workflow YAML loads cleanly via ``Workflow.from_config`` — 3 child
  steps, 4 DirectLinks, 1 input DU, 1 output DU.
* The WorkflowBuilder programmatic variant produces an
  semantically-equivalent Workflow (same steps, same link count, same
  IO DU names) — proves the two authoring paths don't drift.
* Each step's ``process()`` contract: shape-checks at boundaries
  FAIL-FAST with actionable error messages; the happy-path
  pass-through works on fixture inputs.
* AggregateReportStep correctly threads ToolPrewarmResult dataclass
  instances through to the final PrewarmReport.

Live-install paths are exercised by the integration test
``tests/integration/test_prewarm_workflow_live.py`` which drives the
full cascade against real Postgres + Redis + rhea venv.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.infrastructure.prewarm_workflow import (
    AggregateReportStep,
    CollectToolsStep,
    InstallToolsStep,
)
from apecx_integration.infrastructure.rhea_prewarm import (
    PrewarmReport,
    ToolPrewarmResult,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_YAML = (
    REPO_ROOT / "src/apecx_integration/infrastructure/prewarm_workflow/configs/prewarm_workflow.yml"
)


# ---------------------------------------------------------------------------
# Workflow YAML loadability + structural shape
# ---------------------------------------------------------------------------


def test_workflow_yaml_loads_via_from_config():
    """The prewarm workflow YAML loads through the canonical path.

    Catches schema drift between the workflow YAML, per-step YAMLs,
    and StepConfig subclasses' ``extra='forbid'`` model_config — the
    dominant silent-failure shape this workspace fights (typos in YAML
    silently using defaults). Also pins the topology: any future
    structural change (renamed step, dropped link, added DU) breaks
    this test as a tripwire.
    """
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(str(WORKFLOW_YAML))
    children = getattr(wf, "child_steps", None) or getattr(wf, "_child_steps", {}) or {}
    assert set(children) == {"collect_tools", "install_tools", "aggregate_report"}, (
        f"unexpected child step set: {sorted(children)!r}"
    )
    assert len(wf.step_links) == 4, f"expected 4 DirectLinks; got {sorted(wf.step_links)!r}"
    assert set(wf.input_data_units) == {"prewarm_request"}
    assert set(wf.output_data_units) == {"prewarm_report"}


def test_workflow_yaml_links_carry_auto_transfer_true():
    """Every DirectLink declares auto_transfer: true (parsed YAML).

    Workspace-known silent-failure shape: a DirectLink without
    auto_transfer registers but never transfers on source-DU change.
    The workflow loads cleanly, the trigger cascade fires, every step
    runs — but no data moves. We parse the YAML and check each link's
    nested config, so the test isn't fooled by ``auto_transfer: true``
    appearing in prose comments.
    """
    import yaml

    parsed = yaml.safe_load(WORKFLOW_YAML.read_text())
    links = parsed.get("links") or {}
    assert links, "workflow YAML has no links: block"
    for link_name, link_entry in links.items():
        cfg = (link_entry or {}).get("config") or {}
        assert cfg.get("auto_transfer") is True, (
            f"link {link_name!r} is missing auto_transfer: true — "
            f"workspace G7 silent-failure guard."
        )


# ---------------------------------------------------------------------------
# WorkflowBuilder programmatic variant — parity with YAML path
# ---------------------------------------------------------------------------


def test_builder_variant_produces_equivalent_workflow():
    """The WorkflowBuilder path produces a Workflow with the same shape
    as the YAML path.

    Doesn't assert literal equality (the names differ — the builder
    auto-names links link_0..link_3 vs the YAML's descriptive names)
    but asserts the workflow's step set, link count, and IO DU names
    match. If the two authoring paths drift, downstream consumers
    that swap between them would get subtly different topologies and
    silent-fail.
    """
    from nanobrain.core.workflow import Workflow

    from apecx_integration.infrastructure.prewarm_workflow.builder import (
        build_prewarm_workflow_via_builder,
    )

    yaml_wf = Workflow.from_config(str(WORKFLOW_YAML))
    builder_wf = build_prewarm_workflow_via_builder()

    def _children(wf):
        return getattr(wf, "child_steps", None) or getattr(wf, "_child_steps", {}) or {}

    assert set(_children(yaml_wf)) == set(_children(builder_wf)), (
        "step set differs between YAML and builder paths"
    )
    assert len(yaml_wf.step_links) == len(builder_wf.step_links) == 4
    assert set(yaml_wf.input_data_units) == set(builder_wf.input_data_units)
    assert set(yaml_wf.output_data_units) == set(builder_wf.output_data_units)


# ---------------------------------------------------------------------------
# Per-step contract tests
# ---------------------------------------------------------------------------


def _load_step(cls, config_filename: str):
    """Load a step from its per-step YAML config — the canonical path."""
    cfg_path = (
        REPO_ROOT
        / "src/apecx_integration/infrastructure/prewarm_workflow/configs"
        / config_filename
    )
    return cls.from_config(str(cfg_path))


def test_collect_tools_step_rejects_non_dict_input():
    """CollectToolsStep.process FAIL-FASTs on non-dict input.

    Anti-silent-failure: if some upstream change pipes the wrong shape
    into the workflow input DU, the step refuses immediately with an
    actionable error pointing at the orchestrator's request-composer.
    """
    step = _load_step(CollectToolsStep, "collect_tools_step.yml")
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(step.process({"prewarm_request": "not a dict"}))
    assert "expected input_data['prewarm_request'] to be a dict" in str(exc_info.value)


def test_collect_tools_step_rejects_missing_required_keys():
    """CollectToolsStep names the missing key(s), not just 'invalid'."""
    step = _load_step(CollectToolsStep, "collect_tools_step.yml")
    with pytest.raises(ValueError) as exc_info:
        # Missing database_url + redis_host + redis_port.
        asyncio.run(step.process({"prewarm_request": {"catalog_path": None}}))
    err = str(exc_info.value)
    assert "missing required keys" in err
    assert "database_url" in err
    assert "redis_host" in err
    assert "redis_port" in err


def test_install_tools_step_rejects_non_dict_input():
    """InstallToolsStep FAIL-FASTs on non-dict input — same discipline."""
    step = _load_step(InstallToolsStep, "install_tools_step.yml")
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(step.process({"collect_tools_output": "not a dict"}))
    assert "expected input_data['collect_tools_output'] to be a dict" in str(exc_info.value)


def test_install_tools_step_handles_empty_tool_names():
    """An empty tool list emits an empty results list — no error.

    A catalog with no prewarm_rhea_tools entries is a legitimate
    operator choice (e.g., during development where tool installs are
    handled manually). The step must accept it.
    """
    step = _load_step(InstallToolsStep, "install_tools_step.yml")
    out = asyncio.run(
        step.process(
            {
                "collect_tools_output": {
                    "tool_names": [],
                    "install_config": {
                        "database_url": "postgresql://example/db",
                        "redis_host": "localhost",
                        "redis_port": 6379,
                        "rhea_python": None,
                    },
                }
            }
        )
    )
    assert out == {"install_tools_output": {"results": []}}


def test_aggregate_report_step_builds_report_with_correct_all_ready():
    """AggregateReportStep correctly aggregates the all_ready predicate."""
    step = _load_step(AggregateReportStep, "aggregate_report_step.yml")
    results = [
        ToolPrewarmResult(tool_name="a", state="ready", latency_seconds=10.0),
        ToolPrewarmResult(tool_name="b", state="reused", latency_seconds=0.1),
    ]
    out = asyncio.run(step.process({"install_tools_output": {"results": results}}))
    report = out["prewarm_report"]
    assert isinstance(report, PrewarmReport)
    assert report.all_ready is True
    assert len(report.tools) == 2


def test_aggregate_report_step_all_ready_false_on_any_failed():
    """A single failed tool flips all_ready to False."""
    step = _load_step(AggregateReportStep, "aggregate_report_step.yml")
    results = [
        ToolPrewarmResult(tool_name="a", state="ready", latency_seconds=10.0),
        ToolPrewarmResult(tool_name="b", state="failed", error="boom"),
    ]
    out = asyncio.run(step.process({"install_tools_output": {"results": results}}))
    report = out["prewarm_report"]
    assert isinstance(report, PrewarmReport)
    assert report.all_ready is False


def test_aggregate_report_step_rejects_dict_in_results_list():
    """If a result was JSON round-tripped to a dict, FAIL-LOUD.

    The orchestrator's status() iterates ``report.tools`` calling
    ``.state`` attribute access — a dict would surface as an
    AttributeError downstream. The step catches the shape drift at
    the boundary where the original type is still recoverable.
    """
    step = _load_step(AggregateReportStep, "aggregate_report_step.yml")
    results = [{"name": "a", "state": "ready"}]  # dict, not ToolPrewarmResult
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(step.process({"install_tools_output": {"results": results}}))
    err = str(exc_info.value)
    assert "results[0]" in err
    assert "ToolPrewarmResult" in err
