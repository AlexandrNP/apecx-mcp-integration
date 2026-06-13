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

``harmonized_search`` is now the sole entry point on the MCP surface
for "find records about X in an APECx Globus index". The prior raw
free-text passthrough ``query_globus_search`` was deregistered from
the MCP wire on 2026-06-09 because it bypassed the synonym dictionary
+ HITL gate entirely (see ``tools/_hitl_gate.py`` for the
architectural rationale + ``server.py`` for the deregistration
comment). The Python function remains importable for internal callers
(the synthesis pipeline still composes Globus results internally).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from apecx_integration.composition.runtime.observed_run import (
    run_workflow_observed,
)

log = logging.getLogger(__name__)


# Taxonomy-harmonized indices — mirror composition/steps/harmonized_search_execute_step.py
# _HARMONIZED_FILTER. These go through the resolve→execute workflow (synonym
# dictionary + raw-vs-harmonized divergence). Duplicated rather than imported to
# keep the MCP tool's param check local and tight.
_TAXONOMY_INDICES = {
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

# Aggregate-served STRUCTURAL indices (2026-06-12 decision α). PDB + EMDB are
# already DataCite-harmonized inside the aggregate index e74bf12a (Phase 0 probe:
# 27,407 + 8,360 records), separable by the verified ``publisher.name``
# discriminator. They do NOT carry a taxon/synonym harmonization model, so they
# bypass the resolve→execute taxonomy workflow entirely and run a single
# publisher-scoped structural query here at the tool boundary — keeping the
# 9-index taxonomy pipeline (and its dest-alignment invariant) untouched.
_AGGREGATE_SERVED = {
    "pdb": "RCSB PDB",
    "emdb": "Electron Microscopy Data Bank",
}

_VALID_INDICES = _TAXONOMY_INDICES | set(_AGGREGATE_SERVED)

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


async def _aggregate_served_search(term: str, index: str) -> dict:
    """Structural search for an aggregate-served index (pdb/emdb).

    Queries the aggregate Globus index (e74bf12a) with a ``publisher.name`` filter
    scoping to the single structural source, plus the user ``term`` as free text.
    No synonym/taxon resolution and no raw-vs-harmonized divergence — those are
    taxonomy concepts that do not apply to structures. Returns a WorkflowResult-
    shaped dict matching the harmonized_search contract. A no-hit is LOUD
    (explicit, never a silent empty); a Globus outage is a loud status=error.
    """
    import asyncio  # noqa: PLC0415

    from apecx_integration.agents.globus_search import client as globus_client  # noqa: PLC0415
    from apecx_integration.composition.schemas.workflow_result import (  # noqa: PLC0415
        WorkflowResult,
    )

    publisher = _AGGREGATE_SERVED[index]
    try:
        hits = await asyncio.to_thread(
            globus_client.search,
            term,
            max_results=50,
            filters=[{"type": "match_any", "field_name": "publisher.name", "values": [publisher]}],
        )
    except Exception as exc:  # GlobusSearchUnavailableError + any SDK/network error
        return WorkflowResult.failed(
            f"structural search on index {index!r} (publisher {publisher!r}) failed: "
            f"{type(exc).__name__}: {exc}",
            markdown=(
                f"### Structural search failed on `{index}`\n\n"
                f"Globus query error: {type(exc).__name__}: {exc}"
            ),
        ).model_dump(mode="json")

    if not hits:
        md = (
            f"### Structural search: `{term}` on `{index}`\n\n"
            f"**No records found.** No {publisher} structural records in the APECx "
            f"aggregate corpus matched `{term}`. (This index is publisher-scoped to "
            f"{publisher}; it does not use NCBI-taxonomy harmonization.)"
        )
        return WorkflowResult(markdown=md).model_dump(mode="json")

    md_lines = [
        f"### Structural search: `{term}` on `{index}`",
        "",
        f"**{len(hits)}** {publisher} structural record(s) (publisher-scoped within the "
        f"APECx aggregate index):",
        "",
    ]
    from apecx_integration.agents.globus_search._datacite import (  # noqa: PLC0415
        datacite_subjects,
        datacite_title,
    )

    for h in hits:
        subject = h.get("subject") or "(unknown)"
        content = h.get("content") or {}
        # DataCite-shaped records store the title at titles[0].title; reading the
        # flat "title" key rendered every structural hit as "(untitled)".
        title = datacite_title(content)
        subjects = datacite_subjects(content, limit=4)
        kw = f" — {', '.join(subjects)}" if subjects else ""
        md_lines.append(f"- **[Globus {subject}]** *{title or '(untitled)'}*{kw}")
    return WorkflowResult(markdown="\n".join(md_lines)).model_dump(mode="json")


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
        A taxonomy-harmonized index — ``violin_pathogen``,
        ``violin_vaccine``, ``violin_gene``, ``bvbrc_genome``,
        ``bvbrc_protein``, ``bvbrc_protein_structure``,
        ``bvbrc_epitope``, ``antiviraldb``, ``protabank`` (these resolve
        ``term`` through the synonym dictionary and report raw-vs-
        harmonized divergence) — OR an aggregate-served STRUCTURAL index
        ``pdb`` / ``emdb`` (PDB and EMDB records served from the APECx
        aggregate corpus, scoped by ``publisher.name``; ``term`` is a
        free-text structural query, no taxon resolution).
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

    # Aggregate-served structural indices (pdb/emdb) bypass the taxonomy
    # resolve→execute workflow entirely — they have no synonym/taxon model.
    if index in _AGGREGATE_SERVED:
        return await _aggregate_served_search(term, index)

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
