"""Deployment e2e — REAL tool calls using the SIGNATURES THE CODE EXPOSES.

Every call here uses the parameter names the tool actually declares. This is the direct antidote to the
prose runbook's fiction: it documented ``harmonized_search(query=...)`` and ``describe_workflow(
workflow_name=...)``, both of which raise a pydantic "field required" validation error against the real
tools. If a future change drifts a param name, one of these tests fails instead of an operator's call.
"""

from __future__ import annotations

import pytest


def test_describe_workflow_uses_name_param(call):
    # Runbook said `workflow_name`; the tool requires `name`. Describe a workflow that IS in the
    # discovery catalog (rag_e2e_synthesis_workflow) — see the F8 pin for why the flagship isn't.
    payload = call("describe_workflow", {"name": "rag_e2e_synthesis_workflow"})
    assert "validation error" not in str(payload).lower(), f"param drift: {str(payload)[:300]}"
    assert payload.get("error") is None, f"describe_workflow errored: {payload.get('error')}"
    assert payload.get("workflow_name") == "rag_e2e_synthesis_workflow", (
        f"no schema: {str(payload)[:200]}"
    )


def test_inspect_workflow_lightweight_callable_defers_to_inspect_run(call):
    # A promoted/lightweight workflow is not statically YAML-inspectable; inspect_workflow returns a
    # structured hint to run it + use inspect_run (real behavior, pinned).
    payload = call("inspect_workflow", {"name": "viral_epitope_analysis"})
    assert "validation error" not in str(payload).lower(), f"param drift: {str(payload)[:300]}"
    err = str(payload.get("error") or "")
    assert "inspect_run" in err or payload.get("error") is None, (
        f"unexpected inspect result: {str(payload)[:250]}"
    )


def test_harmonized_search_requires_term_and_index(call, dict_or_skip):
    # Runbook said harmonized_search(query="a virus name"); the tool requires (term, index) and
    # rejects an unknown index. `bvbrc_genome` is a real index; the search hits the public Globus
    # aggregate anonymously (no local data needed).
    payload = call("harmonized_search", {"term": "Zika virus", "index": "bvbrc_genome"})
    low = str(payload).lower()
    assert "field required" not in low, f"wrong signature surfaced: {str(payload)[:300]}"
    assert "not a valid index" not in low and "invalid index" not in low, (
        f"bvbrc_genome rejected as an index: {str(payload)[:300]}"
    )
    assert isinstance(payload, dict), f"expected a structured result, got {type(payload)}"


def test_harmonized_search_rejects_the_runbook_signature(call):
    # Proves WHY the runbook example failed: `query` is not a parameter — the tool raises a validation
    # error (ToolError) rather than silently returning. No dict needed; it fails before any search.
    with pytest.raises(Exception) as exc:  # noqa: PT011 - FastMCP wraps as ToolError; message is what matters
        call("harmonized_search", {"query": "Zika virus"})
    msg = str(exc.value).lower()
    assert "field required" in msg or "term" in msg or "index" in msg, (
        f"expected a (term/index) validation error, got: {msg[:200]}"
    )
