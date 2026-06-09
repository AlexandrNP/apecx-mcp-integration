"""MCP tool — drive the harmonized_search nanobrain workflow.

Wraps the ``composition/workflows/harmonized_search/`` workflow as a
scientist-facing MCP tool. The workflow:

1. Resolves the user term via the synonym dictionary
2. If ambiguous → emits a paused envelope WITHOUT running Globus queries
3. Otherwise → runs raw + harmonized Globus queries, computes
   divergence, emits an ok envelope

This tool sits at the MCP boundary: it loads the workflow once (cached
singleton), runs it through ``run_workflow_observed`` (EO-03), and
returns the ``WorkflowResult`` envelope as a JSON-serializable dict.

When ``index`` is invalid the tool raises ``ValueError`` with the
expected enum echoed back (Audit §3.10) — Pydantic would otherwise
surface a generic message deep in the step.

Why this lives next to ``query_globus_search`` rather than replacing
it: ``query_globus_search`` is a raw free-text passthrough useful for
exploratory Lucene-syntax queries. ``harmonized_search`` is the
opinionated harmonization path: term → canonical IRI → per-index
filter → raw-vs-harmonized comparison with HITL gating on ambiguous
resolution. Both should be on the wire.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from apecx_integration.composition.runtime.observed_run import (
    run_workflow_observed,
)

log = logging.getLogger(__name__)


# Valid indices — mirrors composition/steps/harmonized_search_execute_step.py
# _HARMONIZED_FILTER. Duplicated rather than imported to keep the MCP
# tool's "what valid params can I pass" check local and tight.
_VALID_INDICES = {
    "violin_pathogen",
    "violin_vaccine",
    "violin_gene",
    "bvbrc_genome",
    "bvbrc_protein",
    "bvbrc_protein_structure",
    "bvbrc_epitope",
    "antiviraldb",
    "protabank",
}

_VALID_ENTITY_TYPES = {"pathogen", "vaccine", "disease", "gene", ""}


_WORKFLOW_SINGLETON: Any = None
_WORKFLOW_LOAD_ERROR: str | None = None


def _workflow_dir() -> Path:
    """Locate the harmonized_search workflow directory.

    Walk up from this file (mcp_surface/tools/harmonized_search.py) to
    the apecx_integration package root, then descend into the workflow
    directory.

    Path math: __file__ is at apecx_integration/mcp_surface/tools/...,
    so parents[2] is the apecx_integration package root, which holds
    composition/workflows/harmonized_search/.
    """
    here = Path(__file__).resolve()
    return here.parents[2] / "composition" / "workflows" / "harmonized_search"


def _load_workflow() -> Any:
    """Lazy-load and cache the harmonized_search workflow.

    Caches a single Workflow instance for the MCP server's lifetime;
    re-runs each call drive it through new triggers, but the YAML +
    component graph is only parsed once.
    """
    global _WORKFLOW_SINGLETON, _WORKFLOW_LOAD_ERROR
    if _WORKFLOW_SINGLETON is not None:
        return _WORKFLOW_SINGLETON
    if _WORKFLOW_LOAD_ERROR is not None:
        # Don't keep retrying a known-bad load.
        raise RuntimeError(_WORKFLOW_LOAD_ERROR)

    from nanobrain.core.workflow import Workflow  # noqa: PLC0415

    wf_yaml = _workflow_dir() / "harmonized_search_workflow.yml"
    if not wf_yaml.is_file():
        _WORKFLOW_LOAD_ERROR = f"harmonized_search workflow YAML missing at {wf_yaml}"
        raise RuntimeError(_WORKFLOW_LOAD_ERROR)

    try:
        _WORKFLOW_SINGLETON = Workflow.from_config(wf_yaml)
    except Exception as exc:  # noqa: BLE001 — surface the cause cleanly
        _WORKFLOW_LOAD_ERROR = (
            f"failed to load harmonized_search workflow from {wf_yaml}: {type(exc).__name__}: {exc}"
        )
        log.exception("harmonized_search workflow load failed")
        raise RuntimeError(_WORKFLOW_LOAD_ERROR) from exc

    log.info("harmonized_search workflow loaded from %s", wf_yaml)
    return _WORKFLOW_SINGLETON


async def harmonized_search(
    term: str,
    index: str,
    entity_type: str = "",
) -> dict:
    """Run the harmonized_search workflow and return its WorkflowResult envelope.

    Drives a three-step nanobrain workflow:
      1. Resolve ``term`` to a canonical IRI via the synonym dictionary
      2. Either emit a *paused* envelope (when the resolution is
         ambiguous — the LLM gets the candidate list, NO Globus
         queries are run, no record count to silently mis-attribute),
         OR run both a raw substring query (``q=<term>``) and a
         harmonized query (filter on per-index field with the
         canonical_label / synonyms / NCBI taxon) and compute
         divergence
      3. Wrap the result into a WorkflowResult envelope

    Parameters
    ----------
    term:
        The user surface form: an acronym (``"CHIKV"``), a verbose
        species name (``"Chikungunya virus"``), or a canonical IRI
        (``"http://purl.obolibrary.org/obo/NCBITaxon_37124"``). On a
        re-call after disambiguation, pass the chosen candidate IRI
        as ``term`` — the resolver short-circuits IRI inputs via
        ``path=fast``.
    index:
        One of the 9 APECx Globus indices: ``violin_pathogen``,
        ``violin_vaccine``, ``violin_gene``, ``bvbrc_genome``,
        ``bvbrc_protein``, ``bvbrc_protein_structure``,
        ``bvbrc_epitope``, ``antiviraldb``, ``protabank``.
    entity_type:
        Optional restrict-by-type hint: ``"pathogen"`` / ``"vaccine"``
        / ``"disease"`` / ``"gene"``. Empty string = search all types.

    Returns
    -------
    dict
        Serialized WorkflowResult — ``{markdown, data_handle,
        data_preview, run_id}``. The ``data_preview`` carries a
        Bundle preview with the resolution path, raw and harmonized
        record counts (or candidate list when paused), and divergence
        stats. The full structured payload sits behind ``data_handle``
        in the HandleStore.
    """
    if not isinstance(term, str) or not term.strip():
        raise ValueError(f"term={term!r} must be a non-empty string.")
    if index not in _VALID_INDICES:
        raise ValueError(
            f"index={index!r} is not a valid Globus index; expected "
            f"one of {sorted(_VALID_INDICES)}."
        )
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(
            f"entity_type={entity_type!r} is not valid; expected one "
            f"of {sorted(_VALID_ENTITY_TYPES - {''})} or '' for any-type."
        )

    workflow = _load_workflow()
    # Input must be keyed by the workflow's input data unit name
    # (workflow_input). The framework's Workflow.process() validates
    # the keys at the boundary and refuses to advance on a mismatch.
    input_data: dict[str, Any] = {
        "workflow_input": {
            "term": term,
            "index": index,
            "entity_type": entity_type,
        }
    }

    outcome = await run_workflow_observed(workflow, input_data)

    if outcome.workflow_result is None:
        # Workflow ran but produced no WorkflowResult — return the raw
        # output dict so the caller can see what happened. Honest
        # "the envelope step didn't fire" rather than a synthetic empty.
        return {
            "_no_envelope": True,
            "raw_result": outcome.raw_result,
            "run_summary": outcome.run_summary.model_dump(mode="json")
            if hasattr(outcome.run_summary, "model_dump")
            else None,
        }

    return outcome.workflow_result.model_dump(mode="json")
