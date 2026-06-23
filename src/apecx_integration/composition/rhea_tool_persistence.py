"""Persist an on-demand-synthesized rhea tool step as a committed, portable wrapper YAML.

Per §0 (resource discipline): rhea tool steps are synthesized ON DEMAND (when a
plan needs a capability) and the generated wrapper is committed to git — the repo
IS the reuse cache. There is no eager bulk synthesis of rhea's ~7000-tool
repertoire and no separate UTD database.

The wrapper is a normal nanobrain step config: reference it from a workflow as
``class: <spec.step_class>`` + ``config: <this file>``. Loadable via
``<spec.step_class>.from_config(<this file>)``.

Reliability invariants:
* **Reproducible-only.** Refuse to persist an UNPINNED spec — a non-version-pinned
  tool would let the committed step drift against whatever the worker happens to
  serve later (a silent reproducibility failure).
* **Portable.** Never bake an environment-specific ``mcp_url`` into a committed
  wrapper; emit ``${RHEA_MCP_URL}`` so it resolves from the env at load time
  (nanobrain YAML env interpolation) — the same portability rule as
  ReasoningPatternStep's search paths.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from nanobrain.library.tools.rhea_step_synthesizer import RheaStepSpec

# Dedicated dir, NOT under composition/workflows/ — these are step wrappers, not
# workflows, and the ``*.yml`` here must never be mistaken for a workflow by the
# discovery scanner / name resolver (which match ``*_workflow.yml``).
_GENERATED_DIR = Path(__file__).resolve().parent / "_generated_tool_steps"


def _slug(descriptor_id: str) -> str:
    """``rhea:muscle@3.8.1551+galaxy0`` -> ``muscle`` (filesystem-safe)."""
    base = descriptor_id.split(":", 1)[-1].split("@", 1)[0]
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_").lower()
    return cleaned or "tool"


def persist_rhea_step(
    spec: RheaStepSpec,
    *,
    dest_dir: Path | None = None,
    tool_slug: str | None = None,
) -> Path:
    """Write *spec* as a portable, committed wrapper YAML; return its path.

    Raises ``ValueError`` for an unpinned spec (non-reproducible). The mcp_url is
    rewritten to ``${RHEA_MCP_URL}`` for portability.
    """
    if not spec.is_pinned:
        raise ValueError(
            f"refusing to persist an UNPINNED rhea step ({spec.descriptor_id!r}): a "
            "non-reproducible version pin would let the committed step drift against "
            "whatever the worker serves later. Synthesize against a provenance-pinned "
            "rhea worker so descriptor_id carries a real version."
        )

    slug = tool_slug or _slug(spec.descriptor_id)
    out_dir = (dest_dir or _GENERATED_DIR) / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    config: dict = {"name": f"{slug}_tool_step"}
    config.update(spec.step_config)
    if "mcp_url" in config:
        config["mcp_url"] = "${RHEA_MCP_URL}"

    header = (
        "# AUTO-GENERATED rhea tool step (on-demand synthesis, git-persisted per §0).\n"
        f"# descriptor: {spec.descriptor_id} (version-pinned)\n"
        f"# step_class: {spec.step_class}\n"
        f"# Reference from a workflow: class: {spec.step_class}, config: this file.\n"
        "# Regenerate via synthesize_rhea_step + persist_rhea_step; do not hand-edit.\n"
    )
    path = out_dir / f"{slug}.yml"
    path.write_text(header + yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


__all__ = ["persist_rhea_step"]
