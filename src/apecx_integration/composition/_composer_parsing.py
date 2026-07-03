"""Parsing + retry-feedback helpers extracted from composer.py (G78).

Four free functions + one regex + one constant tuple. All operate on
LLM response strings and ``ComposerResponseError`` shapes — the
parse-retry loop in ``Composer.compose`` uses them to decide whether
to retry and what correction hint to give the LLM.

These were inline in ``composer.py`` prior to 2026-05-16. Extracted
to shrink composer.py + give the parse-feedback contract a
discoverable home. The composer re-exports each symbol so existing
``from apecx_integration.composition.composer import _xxx`` imports
keep working without test changes.
"""

from __future__ import annotations

import logging
import re

import yaml

from apecx_integration.composition._errors import ComposerResponseError

log = logging.getLogger(__name__)


# Matches a fenced block whose label is captured as group 1 and whose
# body is group 2. Handles both ``` and ~~~ fences per CommonMark
# (limited to ``` to keep the regex simple). Greedy on the body with
# a non-greedy-ish trailing fence.
#
# Audit §1.3: ``\n\s*`` before the closing fence (instead of plain
# ``\n```) tolerates trailing whitespace or a blank line between the
# body and the closing fence — valid CommonMark, occasionally
# emitted by LLMs whose training distribution includes that pattern.
# Pre-fix the parser silently failed to match such blocks and the
# composer raised "no ```yaml fenced block" with no hint that the
# block existed but had a trailing blank line.
_FENCE_RE = re.compile(
    r"```\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\n"
    r"(.*?)"
    r"\n\s*```",
    re.DOTALL,
)

# Substrings used to recognize ``ComposerResponseError`` variants the
# compose-validate-retry loop can plausibly repair. Kept frozen so a
# future rewording of the parser's error messages forces an explicit
# update here (and a corresponding test) instead of silently widening
# / narrowing the retry surface.
_REPAIRABLE_PARSE_MARKERS: tuple[str, ...] = (
    "must be a mapping at top level",
    # SPEC2 (2026-05-11): spec-mode parse errors — JSON shape / schema
    # / expander failures are all repairable when the LLM sees the
    # actual error message.
    "spec mode: emitted JSON did not match",
    "spec mode: JSON in the fenced block did not parse",
    "spec mode: expander could not realize the spec",
)


def _is_repairable_parse_error(exc: ComposerResponseError) -> bool:
    """True iff the parse failure has a shape the LLM can correct
    given feedback. Empty-content / no-yaml-fence errors are NOT
    repairable — the retry would just re-elicit the same shape."""
    text = str(exc)
    return any(marker in text for marker in _REPAIRABLE_PARSE_MARKERS)


def _format_parse_feedback(exc: ComposerResponseError) -> str:
    """User-turn message for the parse-error retry path.

    Tells the LLM exactly what went wrong shape-wise plus a brief
    example of the expected shape. Kept short — the system prompt
    already carries the full schema; this is just a correction hint.
    """
    return (
        "Your previous response could not be parsed as a workflow "
        "YAML mapping. The composer surfaced this error:\n\n"
        f"    {exc}\n\n"
        "Emit exactly ONE fenced ```yaml``` block whose top level "
        "is a MAPPING with keys like `name:`, `description:`, "
        "`steps:`, `links:`. Do not emit a list, a string, or "
        "multiple yaml blocks. If you intended to provide novel "
        "Python, put it in a separate ```novel_python``` fence "
        "exactly once."
    )


def _is_class_not_found_error(exc: ComposerResponseError) -> bool:
    """True iff the spec realized-but-referenced a step class the catalog does not
    have (an LLM class-name hallucination). Distinct from a YAML-shape parse error:
    the shape was fine, so the generic shape-correction feedback is the WRONG hint —
    the LLM needs the list of valid class names instead."""
    text = str(exc)
    return "expander could not realize the spec" in text and "has no catalog match" in text


def _format_class_not_found_feedback(
    exc: ComposerResponseError, valid_class_names: list[str]
) -> str:
    """Retry feedback for a hallucinated class-name error. Unlike ``_format_parse_feedback``
    (which corrects YAML SHAPE) this names the failure and lists the VALID catalog class
    names so the LLM can pick a real one instead of re-inventing. Caller passes the names
    (this module stays free of the catalog)."""
    names = ", ".join(sorted(valid_class_names)) if valid_class_names else "(catalog empty)"
    return (
        "A step in your workflow referenced a `class_name` that does not exist. The "
        "composer surfaced this error:\n\n"
        f"    {exc}\n\n"
        "The YAML shape was fine — the problem is the class name. Every step's "
        "`class_name` MUST be one of these catalog leaf names (or a full dotted class "
        "path); do NOT invent class names:\n\n"
        f"    {names}\n\n"
        "Re-emit the same workflow with each step's `class_name` replaced by the closest "
        "matching catalog leaf name from that list."
    )


def _parse_response(content: str) -> tuple[str, dict[str, str]]:
    """Extract the single ``yaml`` fenced block and the optional
    ``novel_python`` fenced block from the LLM response.

    Returns ``(yaml_text, novel_python_dict)``. Raises
    ``ComposerResponseError`` if the yaml block is missing.

    Strictness: we accept the LLM emitting prose outside fences
    (retryable) but reject the absence of ANY yaml fence entirely
    (unparseable). Multiple yaml fences → first one wins; we log the
    extras but don't error — LLMs sometimes emit a second yaml as a
    "preview" and we'd rather take the first than fail.
    """
    blocks: dict[str, list[str]] = {}
    for match in _FENCE_RE.finditer(content):
        label = match.group(1).lower()
        body = match.group(2)
        blocks.setdefault(label, []).append(body)

    yaml_blocks = blocks.get("yaml", [])
    if not yaml_blocks:
        raise ComposerResponseError(
            f"LLM response has no ```yaml fenced block. First 500 chars: {content[:500]!r}"
        )
    if len(yaml_blocks) > 1:
        log.warning(
            "Composer response has %d yaml blocks; using the first",
            len(yaml_blocks),
        )
    yaml_text = yaml_blocks[0]

    novel_python_raw = blocks.get("novel_python", [])
    novel_python: dict[str, str] = {}
    if novel_python_raw:
        # novel_python is itself a YAML mapping step_id -> source.
        try:
            parsed = yaml.safe_load(novel_python_raw[0])
        except yaml.YAMLError as exc:
            raise ComposerResponseError(
                f"LLM response's novel_python block failed to parse: {exc}"
            ) from exc
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise ComposerResponseError(
                "LLM response's novel_python block must be a mapping "
                f"<step_id>: <source>; got {type(parsed).__name__}"
            )
        for k, v in parsed.items():
            if not isinstance(v, str):
                raise ComposerResponseError(
                    f"novel_python[{k!r}] must be a source string; got {type(v).__name__}"
                )
            novel_python[str(k)] = v

    return yaml_text, novel_python


__all__ = [
    "_FENCE_RE",
    "_REPAIRABLE_PARSE_MARKERS",
    "_format_parse_feedback",
    "_format_class_not_found_feedback",
    "_is_class_not_found_error",
    "_is_repairable_parse_error",
    "_parse_response",
]
