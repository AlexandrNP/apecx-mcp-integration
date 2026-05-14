"""MCP tool for the rhea_muscle_alignment workflow.

Exposes the three-step ``rhea_muscle_alignment`` pipeline (collect FASTA
→ run MUSCLE on Rhea over MCP → report the alignment) as a single
one-shot MCP tool. Use when the operator wants a direct "align these
sequences with MUSCLE" path without going through workflow composition.

Invocation paths for the same pipeline
--------------------------------------
1. This ``align_sequences_with_muscle`` MCP tool — canonical, cached
   process-wide, drives the three step classes directly.
2. ``Workflow.from_config(rhea_muscle_alignment/workflow.yml)`` — the
   nanobrain triggers + links runtime.
3. Direct step ``from_config`` + ``process()`` calls.

All three are covered by
``tests/integration/test_rhea_muscle_alignment_workflow.py``.

The three step instances are cached as module-level singletons so a
long-running MCP server doesn't re-load the step configs on every call.

RUN PREREQUISITE: the MUSCLE step needs the ``rhea`` repo on
PYTHONPATH + a reachable Rhea MCP server + Redis. When those are
absent the tool returns ``{"error": ...}`` with the FAIL-LOUD message
from the step — it never silently returns an empty result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_COLLECTION_STEP: Any = None
_MUSCLE_STEP: Any = None
_REPORT_STEP: Any = None
_LOAD_ERROR: str | None = None


def _workflow_dir() -> Path:
    """Locate the rhea_muscle_alignment workflow directory.

    Mirrors ``synthesis.py``: this file is at
    ``mcp_surface/tools/muscle_alignment.py`` so the repo's
    ``apecx_integration`` package root is ``parents[2]`` and the
    workflow lives under ``composition/workflows/``.
    """
    here = Path(__file__).resolve()
    # mcp_surface/tools/muscle_alignment.py → parents[2] = apecx_integration
    return here.parents[2] / "composition" / "workflows" / "rhea_muscle_alignment"


def _load_steps() -> tuple[Any, Any, Any] | tuple[None, None, None]:
    """Lazy-load and cache the three step instances.

    Returns ``(collection, muscle, report)`` on success or
    ``(None, None, None)`` when the load failed; the caller checks the
    module-level ``_LOAD_ERROR`` for the reason.
    """
    global _COLLECTION_STEP, _MUSCLE_STEP, _REPORT_STEP, _LOAD_ERROR
    if _COLLECTION_STEP is not None and _MUSCLE_STEP is not None and _REPORT_STEP is not None:
        return _COLLECTION_STEP, _MUSCLE_STEP, _REPORT_STEP
    if _LOAD_ERROR is not None:
        return None, None, None

    try:
        from nanobrain.core.step import BaseStep

        wf_dir = _workflow_dir()
        collection_yaml = wf_dir / "steps" / "fasta_collection.yml"
        muscle_yaml = wf_dir / "steps" / "muscle_alignment.yml"
        report_yaml = wf_dir / "steps" / "alignment_report.yml"

        for yaml_path in (collection_yaml, muscle_yaml, report_yaml):
            if not yaml_path.is_file():
                _LOAD_ERROR = (
                    f"rhea_muscle_alignment step YAML not found at "
                    f"{yaml_path}. The workflow must be present in this "
                    f"checkout for align_sequences_with_muscle to work."
                )
                return None, None, None

        _COLLECTION_STEP = BaseStep.from_config(str(collection_yaml))
        _MUSCLE_STEP = BaseStep.from_config(str(muscle_yaml))
        _REPORT_STEP = BaseStep.from_config(str(report_yaml))
        log.info("align_sequences_with_muscle: loaded collection + muscle + report steps")
    except Exception as exc:  # noqa: BLE001 — final user-facing fallback
        _LOAD_ERROR = f"Failed to load rhea_muscle_alignment pipeline: {type(exc).__name__}: {exc}"
        log.warning(_LOAD_ERROR)
        return None, None, None

    return _COLLECTION_STEP, _MUSCLE_STEP, _REPORT_STEP


async def align_sequences_with_muscle(
    fasta_path: str | None = None,
    fasta_text: str | None = None,
) -> dict:
    """Run the MUSCLE multiple-sequence-alignment pipeline on a FASTA.

    Drives the three-step ``rhea_muscle_alignment`` workflow directly:
    collect the FASTA → dispatch the MUSCLE tool to Rhea over MCP →
    parse the alignment and report statistics. When neither argument
    is supplied, the bundled example FASTA (``data/seqtest.fasta``,
    5 protein sequences) is used.

    Args:
        fasta_path: Path to a FASTA file to align. Optional.
        fasta_text: Raw FASTA text to align. Optional. Takes priority
            over ``fasta_path`` when both are given.

    Returns:
        On success: ``{"summary": "<text>", "n_sequences": <int>,
        "alignment_length": <int>, "alignment_fasta": "<aligned FASTA>"}``.
        On error: ``{"error": "<message>"}`` — a missing ``rhea`` repo,
        an unreachable Rhea MCP server / Redis, a non-zero MUSCLE
        return code, or an empty-output gate failure all surface this
        way; the MCP client should display the message to the operator.
    """
    collection, muscle, report = _load_steps()
    if collection is None or muscle is None or report is None:
        return {"error": _LOAD_ERROR or "rhea_muscle_alignment pipeline not loaded"}

    request: dict[str, Any] = {}
    if isinstance(fasta_text, str) and fasta_text.strip():
        request["fasta_text"] = fasta_text
    elif isinstance(fasta_path, str) and fasta_path.strip():
        request["fasta_path"] = fasta_path

    try:
        staged = await collection.process(request)
        tool_result = await muscle.process(staged)
        report_result = await report.process(tool_result)
    except Exception as exc:  # noqa: BLE001 — surface every failure shape
        # RheaFileToolStep raises ComponentConfigurationError (FAIL-LOUD)
        # on a missing rhea repo, unreachable server, non-zero return
        # code, or empty output; FastaCollectionStep / AlignmentReportStep
        # raise ValueError on bad inputs. Surface the message verbatim.
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "summary": report_result.get("summary", ""),
        "n_sequences": report_result.get("n_sequences", 0),
        "alignment_length": report_result.get("alignment_length", 0),
        "alignment_fasta": report_result.get("alignment_fasta", ""),
    }


__all__ = ["align_sequences_with_muscle"]
