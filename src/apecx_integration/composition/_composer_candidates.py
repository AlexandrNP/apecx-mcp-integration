"""Candidate-rendering + step-IO inspection helpers extracted from composer.py (G78).

Three free functions that format retrieval hits + inspect wrapper
YAMLs for input/output data unit names. Used by ``Composer`` to
render the "here are the catalog components you can use" block
shown to the LLM.

Two render modes:

* ``_render_candidates`` — full mode with ``emit_step:`` ready-to-
  paste step shape. Used by the monolithic composer prompt.
* ``_render_candidates_spec`` — compact spec-mode rendering with
  ``class_name`` + ``inputs`` + ``outputs`` only. Used by the
  SPEC2 composer mode.

Extracted 2026-05-16 from composer.py. Re-exported from composer.py
so existing test imports
(``from apecx_integration.composition.composer import _render_candidates``)
keep working without change.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:  # pragma: no cover — type-checker only
    from apecx_integration.composition.component_catalog import SearchHit


def _load_step_io_names(absolute_yaml_path: str | None) -> tuple[list[str], list[str]]:
    """Read a wrapper YAML and return its input/output data unit names.

    SPEC2 (2026-05-11): the spec-mode candidate block surfaces these
    names so the LLM can use them as link endpoints WITHOUT having
    to guess. Without this, link endpoints are the second-largest
    hallucination surface after class paths.

    The path comes from ``CatalogComponent.yaml_path_absolute``
    (resolved at catalog-load from manifest_dir + relative yaml).
    Returns ``([], [])`` when the file isn't readable or doesn't
    have the standard blocks.
    """
    if not absolute_yaml_path:
        return ([], [])
    p = Path(absolute_yaml_path)
    if not p.is_file():
        return ([], [])
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return ([], [])
    if not isinstance(raw, dict):
        return ([], [])
    inputs = list((raw.get("input_data_units") or {}).keys())
    outputs = list((raw.get("output_data_units") or {}).keys())
    return (inputs, outputs)


def _render_candidates_spec(hits: list[SearchHit]) -> str:
    """Render retrieval hits in the compact spec-mode format.

    Each candidate is reduced to:

        - class_name: LeafName              # the LLM emits this verbatim
          class_path: full.dotted.LeafName  # for the reviewer's eye
          description: ...
          inputs: [data_unit_name_a, ...]   # link endpoints
          outputs: [data_unit_name_b, ...]

    The expander rebuilds full YAML; the LLM does NOT see the
    framework's boilerplate fields. Fewer fields = smaller
    hallucination surface.
    """
    lines: list[str] = []
    for hit in hits:
        c = hit.component
        leaf = c.class_path.rsplit(".", 1)[-1] if c.class_path else c.id
        lines.append(f"- class_name: {leaf}")
        if c.class_path:
            lines.append(f"  class_path: {c.class_path}")
        if c.description:
            lines.append(f"  description: {c.description}")
        inputs, outputs = _load_step_io_names(c.yaml_path_absolute)
        if inputs:
            lines.append(f"  inputs:  {inputs}")
        if outputs:
            lines.append(f"  outputs: {outputs}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_candidates(hits: list[SearchHit]) -> str:
    """Render retrieval hits as a compact, LLM-consumable block.

    B1 (2026-05-11): each candidate with a wrapper YAML now carries
    an ``emit_step`` block that shows the LLM exactly what to paste
    into ``steps:`` for that component. The block is YAML-formatted
    and uses the canonical class path + canonical config path, so the
    "what should I literally write?" answer is two lines below the
    description. This addresses the recurring drift pattern where
    the LLM saw ``yaml: steps/foo.yml`` but still synthesized an
    inline dict because it had to assemble the step shape itself
    from prose rules.
    """
    lines: list[str] = []
    for hit in hits:
        c = hit.component
        lines.append(f"- id: {c.id}")
        lines.append(f"  name: {c.name}")
        lines.append(f"  class: {c.class_path}")
        if c.yaml_path:
            lines.append(f"  yaml: {c.yaml_path}")
        lines.append(f"  description: {c.description}")
        if c.examples:
            lines.append(f"  examples: {list(c.examples)}")
        if c.yaml_path:
            # Ready-to-paste step shape using a short, semantic
            # step_id derived from the component name. The LLM is
            # expected to swap the step_id for a task-appropriate
            # one — the literal strings to copy are the class path
            # and the config path.
            stub_id = c.name.lower().replace(" ", "_").replace("-", "_")
            lines.append("  emit_step: |")
            lines.append(f"    {stub_id}:")
            lines.append(f"      class: {c.class_path}")
            lines.append(f'      config: "{c.yaml_path}"')
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = [
    "_load_step_io_names",
    "_render_candidates",
    "_render_candidates_spec",
]
