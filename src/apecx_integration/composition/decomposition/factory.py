"""Assemble a production LocalDecomposer from the real workflow catalog (EO capstone item 1).

Wires the §6 local bounded-decomposition fallback with REAL components:
- ``KeywordWorkflowMatcher`` over the live ``workflow_registry`` catalog descriptions,
- ``RunWorkflowDispatcher`` whose loader resolves a name → runnable ``Workflow`` AND whose
  envelope-resolver applies each entry's ``input_envelope_key`` (so a structured task payload
  is deposited where ``Workflow.run`` expects it — no silent empty run),
- ``LLMTaskDecomposer`` (the real ``build_chat_llm`` / ``APECX_LLM_*``) for the fallback split.

This is the deterministic-execution + bounded-fallback engine the external orchestrator hands a
subtask to when it is not itself a single workflow. It is NOT a new MCP surface tool — the §4
surface stays thin (discover/run/inspect/compose/context); this is an internal capability.

KNOWN GAP (flagged, needs a design decision): the matcher matches a workflow from a free-text
task, but a structured-input workflow (e.g. viral_conserved_sites needs {taxon_id, protein})
still requires those params in ``task.payload``. Extracting params from free text → payload is
unsolved here; the caller must supply the payload. See eo_implementation_log.md.
"""

from __future__ import annotations

from typing import Any


def assemble_local_decomposer(*, match_threshold: float = 0.0, **run_kwargs: Any):
    """Build a LocalDecomposer bound to the live catalog + the real LLM decomposer."""
    from apecx_integration.composition.decomposition.dispatchers import RunWorkflowDispatcher
    from apecx_integration.composition.decomposition.llm_decomposer import LLMTaskDecomposer
    from apecx_integration.composition.decomposition.local_decomposer import LocalDecomposer
    from apecx_integration.composition.decomposition.matchers import KeywordWorkflowMatcher
    from apecx_integration.mcp_surface.workflow_registry import (
        _load_workflow_for_entry,
        load_catalog,
    )

    entries = {e.tool_name: e for e in load_catalog().workflows}
    matcher = KeywordWorkflowMatcher({name: e.description for name, e in entries.items()})

    def _loader(name: str):
        entry = entries.get(name)
        if entry is None:
            raise ValueError(f"unknown workflow {name!r}; known: {sorted(entries)}")
        return _load_workflow_for_entry(entry)

    def _envelope_key(name: str) -> str | None:
        entry = entries.get(name)
        return entry.input_envelope_key if entry else None

    dispatcher = RunWorkflowDispatcher(_loader, input_envelope_resolver=_envelope_key, **run_kwargs)
    decomposer = LLMTaskDecomposer()
    return LocalDecomposer(matcher, decomposer, dispatcher, match_threshold=match_threshold)


__all__ = ["assemble_local_decomposer"]
