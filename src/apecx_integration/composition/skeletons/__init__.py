"""Skeleton library — pre-authored MinimalWorkflowSpec instances.

The LLM-side leverage of the spec mode (SPEC1+SPEC2, 2026-05-11)
shrunk the LLM's job from ~25-line YAML to ~5 JSON fields per step.
Skeletons shrink it further: for the N most common workflow shapes,
the LLM picks ONE NAME instead of constructing a spec at all.

The LLM emits:

    {"skeleton": "synthesis_pipeline"}

The loader expands it into a pre-authored MinimalWorkflowSpec
identical to what the LLM would otherwise have to assemble itself.

Why this is framework-native:
  - Skeleton YAMLs live under ``composition/skeletons/*.yml`` — same
    discipline as workflow manifests.
  - They produce framework-legal workflows via the existing
    ``expand_spec`` path. No new framework primitive required.
  - Operators can author new skeletons without touching code.

When to add a skeleton:
  - A workflow shape that's needed often enough that re-prompting
    the LLM to assemble it is wasteful AND the LLM gets it wrong.
  - The shape is stable (parameters might vary, but the topology
    is fixed).
  - The shape has a clear name a user would think to ask for.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from apecx_integration.composition.workflow_spec import MinimalWorkflowSpec


class Skeleton(BaseModel):
    """One pre-authored skeleton.

    ``name`` is the lookup key the LLM emits (and the catalog
    advertises in the spec_system prompt). ``description`` and
    ``when_to_use`` are the hints the LLM reads to decide between
    skeletons.

    ``spec`` is the embedded ``MinimalWorkflowSpec`` the expander
    realizes when this skeleton is selected. Validating the embedded
    spec at load time catches typos in the skeleton authoring AT
    LOAD, not at compose-time.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str
    when_to_use: str = ""
    spec: MinimalWorkflowSpec


class SkeletonLibrary(BaseModel):
    """All skeletons known to the composer.

    Construction: use ``SkeletonLibrary.from_dir(path)``. Direct
    construction (empty library) is valid; the composer treats an
    empty library as "no skeletons available; emit a spec from
    scratch."
    """

    model_config = ConfigDict(extra="forbid")

    skeletons: dict[str, Skeleton] = Field(default_factory=dict)

    @classmethod
    def from_dir(cls, directory: Path) -> SkeletonLibrary:
        """Load every ``*.yml`` under ``directory`` as a Skeleton.

        Files that fail Pydantic validation are skipped with a
        warning — a malformed skeleton must NOT crash the composer
        at init time. Workspace stability rule: prefer "ignore the
        broken artifact" over "every operator's composer fails to
        load because someone misspelled a key."
        """
        import logging

        log = logging.getLogger(__name__)
        out: dict[str, Skeleton] = {}
        if not directory.is_dir():
            return cls(skeletons=out)
        for p in sorted(directory.glob("*.yml")):
            try:
                raw = yaml.safe_load(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("SkeletonLibrary: %s did not parse as YAML: %s", p, exc)
                continue
            try:
                skel = Skeleton.model_validate(raw)
            except Exception as exc:
                log.warning(
                    "SkeletonLibrary: %s did not validate as Skeleton: %s",
                    p,
                    exc,
                )
                continue
            if skel.name in out:
                log.warning(
                    "SkeletonLibrary: duplicate skeleton name %r in %s (first occurrence kept).",
                    skel.name,
                    p,
                )
                continue
            out[skel.name] = skel
        return cls(skeletons=out)

    def get(self, name: str) -> Skeleton | None:
        return self.skeletons.get(name)

    def names(self) -> list[str]:
        return sorted(self.skeletons.keys())

    def render_prompt_block(self) -> str:
        """Compact prompt fragment listing available skeletons.

        Injected into the spec_system prompt so the LLM can choose
        a skeleton instead of assembling a spec. Format:

            ## Available skeletons
            - name: synthesis_pipeline
              description: ...
              when_to_use: ...

        Empty library returns an empty string (no skeleton section
        shown to the LLM).
        """
        if not self.skeletons:
            return ""
        lines: list[str] = ["## Available skeletons", ""]
        for name in self.names():
            skel = self.skeletons[name]
            lines.append(f"- name: {name}")
            lines.append(f"  description: {skel.description}")
            if skel.when_to_use:
                lines.append(f"  when_to_use: {skel.when_to_use}")
            lines.append("")
        return "\n".join(lines).rstrip()


__all__ = ["Skeleton", "SkeletonLibrary"]
