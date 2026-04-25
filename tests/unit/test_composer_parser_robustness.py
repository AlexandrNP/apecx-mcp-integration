"""Audit cluster J — composer parser robustness.

Two narrow regression guards (docs/codebase_audit_2026_04_24.md):

§1.2 — `_parse_response` is now preceded by an explicit empty-content
       check at the call site. If a future LangChain version returns
       `content=None`, the composer raises a clear
       ``ComposerResponseError`` instead of crashing inside the regex.

§1.3 — `_FENCE_RE` tolerates trailing whitespace / blank lines
       between the body and the closing fence (valid CommonMark,
       occasionally emitted by LLMs).

These tests exercise the parser in isolation; the integration tests
in ``test_composer_phase2.py`` already cover the round-trip path.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.composer import (
    ComposerResponseError,
    _parse_response,
)


def test_parse_response_extracts_yaml_body_with_trailing_blank_line():
    """Pre-fix this raised "no ```yaml fenced block" because
    `\\n```` did not match `\\n\\n```. After §1.3 the regex tolerates
    the trailing blank line.
    """
    raw = "```yaml\nname: x\nsteps: {}\nlinks: {}\n\n```\n"
    yaml_text, novel_python = _parse_response(raw)
    assert yaml_text == "name: x\nsteps: {}\nlinks: {}"
    assert novel_python == {}


def test_parse_response_extracts_yaml_body_with_trailing_spaces():
    """Trailing space-only line before ``` is tolerated."""
    raw = "```yaml\nname: x\nsteps: {}\nlinks: {}\n   \n```\n"
    yaml_text, _ = _parse_response(raw)
    assert yaml_text == "name: x\nsteps: {}\nlinks: {}"


def test_parse_response_still_strict_on_missing_yaml_fence():
    """Audit §1.2-adjacent: if there's no ```yaml fence at all,
    we still raise loudly. Tolerance is for trailing whitespace,
    not for absence of the fence.
    """
    raw = "I forgot to emit a fenced block; sorry."
    with pytest.raises(ComposerResponseError, match="no ```yaml"):
        _parse_response(raw)


def test_parse_response_extracts_novel_python_block_with_blank_line():
    """The same regex change applies to the optional novel_python
    fence; a trailing blank line there mustn't break extraction.
    """
    raw = (
        "```yaml\nname: x\nsteps: {}\nlinks: {}\n```\n\n"
        "```novel_python\n"
        "synth: |\n  pass\n\n"
        "```\n"
    )
    yaml_text, novel_python = _parse_response(raw)
    assert yaml_text.startswith("name: x")
    assert "synth" in novel_python
