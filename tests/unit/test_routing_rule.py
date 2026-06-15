"""Unit tests for the APECx tool-routing rule + its install-time placement.

Guards three things:
  1. The rule text carries the load-bearing directives (call apecx first, before web_search).
  2. ``docs/desktop_routing_instructions.md``'s fenced block stays EQUAL to the shipped
     ``ROUTING_RULE`` constant — the doc cannot drift from what ``apecx-setup`` installs.
  3. ``upsert_routing_block`` is surgical + idempotent: it creates, no-ops when current,
     replaces in place when the rule changes, and never clobbers surrounding content.
"""

from __future__ import annotations

import re
from pathlib import Path

from apecx_integration.cli.routing_rule import (
    MARKER_END,
    MARKER_START,
    ROUTING_RULE,
    manual_paste_guidance,
    render_block,
    upsert_routing_block,
    user_global_claude_md_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs" / "desktop_routing_instructions.md"


def test_routing_rule_carries_the_load_bearing_directives():
    for needle in (
        "apecx_capabilities",
        "list_workflows",
        "web_search",
        "MUST",
        "viral_epitope_evidence_review",
    ):
        assert needle in ROUTING_RULE, needle


def test_doc_fenced_block_matches_the_shipped_constant():
    # The package ships src/; docs/ does not. The doc must QUOTE the same text the installer
    # writes — pin them equal so a docs edit can't silently diverge from behavior.
    text = _DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:[a-zA-Z]*)\n(.*?)```", text, flags=re.DOTALL)
    assert blocks, "no fenced code block found in the routing doc"
    assert blocks[0].strip() == ROUTING_RULE.strip(), (
        "docs/desktop_routing_instructions.md fenced block drifted from ROUTING_RULE — "
        "update the doc to match cli/routing_rule.py (it is the source of truth)."
    )


def test_user_global_path_is_dot_claude_claude_md():
    p = user_global_claude_md_path()
    assert p.name == "CLAUDE.md"
    assert p.parent.name == ".claude"


def test_upsert_creates_then_is_idempotent(tmp_path: Path):
    target = tmp_path / "CLAUDE.md"

    first = upsert_routing_block(target)
    assert "created" in first
    body = target.read_text(encoding="utf-8")
    assert MARKER_START in body and MARKER_END in body
    assert ROUTING_RULE in body

    # Second call: no change, no duplicate block.
    second = upsert_routing_block(target)
    assert "already current" in second
    assert target.read_text(encoding="utf-8") == body
    assert body.count(MARKER_START) == 1


def test_upsert_preserves_surrounding_content(tmp_path: Path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My memory\n\nkeep this line\n", encoding="utf-8")

    upsert_routing_block(target)
    body = target.read_text(encoding="utf-8")
    assert "# My memory" in body
    assert "keep this line" in body
    assert ROUTING_RULE in body


def test_upsert_replaces_stale_block_in_place(tmp_path: Path):
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        f"# before\n\n{MARKER_START}\nOLD STALE RULE TEXT\n{MARKER_END}\n\n# after\n",
        encoding="utf-8",
    )

    summary = upsert_routing_block(target)
    body = target.read_text(encoding="utf-8")
    assert "updated" in summary
    assert "OLD STALE RULE TEXT" not in body
    assert ROUTING_RULE in body
    # Surrounding content survives; exactly one managed block remains.
    assert "# before" in body and "# after" in body
    assert body.count(MARKER_START) == 1 and body.count(MARKER_END) == 1


def test_render_block_is_self_describing():
    block = render_block()
    assert block.startswith(MARKER_START)
    assert block.rstrip().endswith(MARKER_END)
    # Points the reader at the source of truth + the un-automatable account-side caveat.
    assert "routing_rule.py" in block
    assert "Custom\n     Instructions" in block or "Custom Instructions" in block


def test_manual_paste_guidance_names_the_account_side_path_and_rule():
    g = manual_paste_guidance()
    assert "Custom Instructions" in g
    assert "Only fall back to web_search" in g  # the rule's last line is present
