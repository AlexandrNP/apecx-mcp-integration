"""Loadability smoke test for the literature_rag workflow.

Pins the from_config contract WITHOUT running the steps:

  1. ``Workflow.from_config(<abs path>)`` loads the workflow + all three step
     YAMLs + all links without raising.
  2. The loaded workflow has exactly three child steps
     (resolve_organism_step, ontology_filter_step, literature_rag_step).
  3. Every DirectLink in the workflow YAML declares ``auto_transfer: true``
     — the dominant nanobrain silent-failure guard. Verified BOTH by parsing
     the YAML and against the loaded workflow's link objects.

Hermetic: no network, no LLM, no real data. ``Workflow.run`` is intentionally
NOT called here — the end-to-end cascade is exercised by the Ollama-gated
``tests/integration/test_literature_cascade_e2e.py``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from nanobrain.core.workflow import Workflow

WORKFLOW_YAML = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "literature_rag"
    / "literature_rag_workflow.yml"
)


def _direct_link_configs() -> list[dict]:
    """Return the config dict of every DirectLink declared in the workflow YAML."""
    raw = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
    out: list[dict] = []
    for link in raw.get("links", {}).values():
        if isinstance(link, dict) and link.get("class", "").endswith("DirectLink"):
            out.append(link.get("config", {}))
    return out


def test_workflow_yaml_exists():
    assert WORKFLOW_YAML.is_file(), f"missing workflow YAML: {WORKFLOW_YAML}"


def test_every_direct_link_declares_auto_transfer_true():
    """Parse-the-YAML check: #DirectLinks == #auto_transfer:true (dominant silent-failure)."""
    configs = _direct_link_configs()
    assert len(configs) == 4, f"expected 4 DirectLinks, found {len(configs)}"
    with_flag = [c for c in configs if c.get("auto_transfer") is True]
    assert len(with_flag) == len(configs), (
        "every DirectLink MUST declare auto_transfer: true; "
        f"{len(configs) - len(with_flag)} missing it"
    )


def test_workflow_loads_from_config():
    """Workflow.from_config loads the whole skeleton without error."""
    wf = Workflow.from_config(str(WORKFLOW_YAML))
    assert wf is not None


def test_workflow_has_exactly_three_steps():
    wf = Workflow.from_config(str(WORKFLOW_YAML))
    steps = wf.child_steps
    assert set(steps.keys()) == {
        "resolve_organism_step",
        "ontology_filter_step",
        "literature_rag_step",
    }, f"expected exactly the three cascade steps, got {sorted(steps.keys())}"


def test_loaded_link_objects_have_auto_transfer_true():
    """Cross-check against the live link objects, not just the YAML text."""
    wf = Workflow.from_config(str(WORKFLOW_YAML))
    links = wf.step_links
    direct_links = [lk for lk in links.values() if type(lk).__name__ == "DirectLink"]
    assert len(direct_links) == 4, f"expected 4 DirectLinks, got {len(direct_links)}"
    for lk in direct_links:
        assert getattr(lk, "auto_transfer", False) is True, (
            f"DirectLink {lk!r} loaded with auto_transfer != True"
        )
