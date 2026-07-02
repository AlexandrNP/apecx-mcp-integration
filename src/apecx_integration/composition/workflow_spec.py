"""Minimal workflow spec + deterministic template expander.

Architecture pivot (2026-05-11): instead of asking the LLM to emit a
~25-line workflow YAML with 8 different field types (one for every
hallucination shape we've measured), ask it for a TINY JSON spec:

    {
      "name": "my_workflow",
      "description": "...",
      "steps": [
        {"id": "rag_synth", "class_name": "RagSynthesisStep"},
        {"id": "assemble",  "class_name": "SynthesisContextAssemblyStep"}
      ],
      "links": [
        {"source": "workflow_input", "target": "assemble.assembly_input"},
        {"source": "assemble.synthesis_bundle_output",
         "target": "rag_synth.synthesis_input"},
        {"source": "rag_synth.synthesis_markdown_output",
         "target": "workflow_output"}
      ]
    }

The expander handles every deterministic detail:

  - Leaf ``class_name`` → full catalog ``class_path`` + canonical
    wrapper ``config:`` path.
  - Link class default (``nanobrain.core.link.DirectLink``).
  - ``auto_transfer: true`` (forced — the dominant silent-failure
    shape).
  - ``link_type: direct`` (default).
  - Link id auto-generated (``<source>_to_<target>``).
  - Workflow-level ``input_data_units`` / ``output_data_units``
    blocks scaffolded automatically when the user references
    ``workflow_input`` / ``workflow_output`` in any link.

What the LLM still has to know:

  - Which step classes to use (by name, not full path).
  - The link topology (which step output feeds which step input).
  - The data-unit names on each step's wrapper YAML (e.g.,
    ``synthesis_input`` vs ``assembly_input``). We can't infer
    these — they're a step-author choice. The agent's system prompt
    surfaces them as part of the candidate block.

Framework-native: the produced workflow_dict goes through the same
``Workflow.from_config`` path as a hand-authored YAML.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from apecx_integration.composition.component_catalog import (
    CatalogComponent,
)


class WorkflowStepSpec(BaseModel):
    """One step in the minimal spec.

    ``class_name`` is the leaf class name (e.g. ``RagSynthesisStep``)
    OR a full dotted path. The expander resolves the leaf name
    against the catalog; full paths pass through.

    ``config_override`` is optional — when set, overrides the
    catalog's canonical wrapper path. Almost never needed; included
    so a future scientist-authored bespoke config can be wired
    without round-tripping the LLM.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    class_name: str = Field(min_length=1)
    config_override: str | None = None


class WorkflowLinkSpec(BaseModel):
    """One link in the minimal spec.

    ``link_type`` defaults to ``"direct"`` (the only one the
    composer prompt currently allows). Future link kinds (conditional,
    transform — explicitly banned in the composer rules) would
    require a separate intent.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    link_type: str = "direct"


class MinimalWorkflowSpec(BaseModel):
    """The full minimal spec the LLM emits.

    Pydantic ``extra='forbid'`` so a typo'd top-level key (e.g.
    ``stes`` for ``steps``) FAILS LOUDLY at parse instead of
    silently dropping the LLM's output. Workspace memory rule.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    version: str = "0.1.0"
    steps: list[WorkflowStepSpec] = Field(default_factory=list)
    links: list[WorkflowLinkSpec] = Field(default_factory=list)
    # Optional escape hatch for shape-bridging novel Python. Map of
    # step_id -> source. When set, the corresponding step is
    # emitted as a novel-Python step (class path derived from the
    # source) instead of a library reference.
    novel_python: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Expander
# ---------------------------------------------------------------------------


class SpecExpansionError(ValueError):
    """A spec referenced a class that the catalog doesn't have, or
    a step_id that doesn't appear in steps, or some other shape
    inconsistency the expander can't recover from. Surface as a
    framework-rule violation upstream.
    """


# The on-disk proxy every novel step is routed through (#1c) — runs the untrusted novel source in a
# hardened Docker sandbox rather than importing it into the host process.
_SANDBOXED_NOVEL_STEP_CLASS = (
    "apecx_integration.composition.steps.sandboxed_novel_step.SandboxedNovelStep"
)


def expand_spec(
    spec: MinimalWorkflowSpec,
    catalog: list[CatalogComponent],
) -> tuple[dict[str, Any], list[str]]:
    """Expand a minimal spec into a full workflow YAML dict.

    Args:
        spec: parsed ``MinimalWorkflowSpec``.
        catalog: full list of ``CatalogComponent`` known to the
            composer. Used for leaf-class-name resolution.

    Returns:
        ``(workflow_dict, warnings)`` — the dict is ready for
        ``yaml.safe_dump`` then ``Workflow.from_config``. Warnings
        are non-fatal hints (e.g., a step's class resolved by leaf
        match; a link source/target referenced a step not yet
        defined). The composer surfaces warnings in
        ``CompositionSummary.review_notes``.

    Raises:
        SpecExpansionError: when the spec is structurally impossible
            to expand (e.g., references a class name with multiple
            catalog matches and no full-path disambiguator). The
            composer surfaces this as a structured violation so the
            retry loop can engage.
    """
    warnings: list[str] = []
    by_leaf: dict[str, list[CatalogComponent]] = {}
    by_full: dict[str, CatalogComponent] = {}
    # Dedup by class_path BEFORE the leaf index — the same class can
    # appear in multiple workflow manifests with different ``id``s
    # (e.g., RagSynthesisStep used by both violin_bvbrc and
    # rag_e2e_synthesis). Without dedup, the leaf map sees two
    # entries with identical class paths and flags every reference
    # as ambiguous.
    seen_class_paths: set[str] = set()
    for c in catalog:
        if c.class_path in seen_class_paths:
            continue
        seen_class_paths.add(c.class_path)
        by_full[c.class_path] = c
        leaf = c.class_path.rsplit(".", 1)[-1]
        by_leaf.setdefault(leaf, []).append(c)

    # ---- Steps -----------------------------------------------------------
    steps_block: dict[str, dict[str, Any]] = {}
    novel_python_classes: dict[str, str] = {}
    sandboxed_novel_config: dict[str, dict[str, Any]] = {}
    for step in spec.steps:
        step_id = step.id

        # Novel-Python path: caller provided source for this step. Route it through the on-disk
        # SandboxedNovelStep proxy (#1c) — the spec's class_name is a bare, unresolvable name whose
        # implementation lives in the untrusted novel_python fence; we NEVER import that into the host.
        # The proxy ships the source into the hardened container instead. Config is a FILE PATH (a
        # BaseStep cannot take inline config — G121); the executor stager materializes steps/<id>.yml
        # from `_apecx_sandboxed_novel_config` (threaded below, kept in the persisted YAML) at run time.
        if step_id in spec.novel_python:
            steps_block[step_id] = {
                "class": _SANDBOXED_NOVEL_STEP_CLASS,
                "config": f"steps/{step_id}.yml",
            }
            sandboxed_novel_config[step_id] = {
                # Self-describing config file (matches the catalog steps/<name>.yml format: class +
                # name + fields at top level). SandboxedNovelStepConfig._strip_framework_keys pops
                # `class` before validation.
                "class": _SANDBOXED_NOVEL_STEP_CLASS,
                "name": step_id,
                "novel_source": spec.novel_python[step_id],
                "target_class_name": step.class_name,
                "step_config": step.config_override or {},
            }
            novel_python_classes[step_id] = spec.novel_python[step_id]
            continue

        # Library path: resolve class_name to a catalog entry.
        component = _resolve_step_class(step.class_name, by_full, by_leaf, warnings, step_id)
        config_value: Any
        if step.config_override is not None:
            config_value = step.config_override
        elif component.yaml_path:
            config_value = component.yaml_path
        else:
            # No canonical wrapper; emit an empty inline dict — only
            # legal for DataUnit/Link/Trigger subclasses. The A1
            # validator catches this if the class is a Step.
            config_value = {}
        steps_block[step_id] = {
            "class": component.class_path,
            "config": config_value,
        }

    # ---- Links + workflow-level data units ------------------------------
    links_block: dict[str, dict[str, Any]] = {}
    wf_input_dus: set[str] = set()
    wf_output_dus: set[str] = set()
    for idx, link in enumerate(spec.links):
        link_id = _make_link_id(link.source, link.target, idx)
        link_class = "nanobrain.core.link.DirectLink"
        # Per the composer's prompt rules: TransformLink banned.
        # Only DirectLink is legal. Surface a warning if the spec
        # asked for something else; the validator will reject anyway.
        if link.link_type and link.link_type != "direct":
            warnings.append(
                f"link {link_id!r} requested link_type="
                f"{link.link_type!r}; only 'direct' is allowed — "
                "forcing to direct."
            )
        links_block[link_id] = {
            "class": link_class,
            "config": {
                "link_type": "direct",
                "source": link.source,
                "target": link.target,
                "auto_transfer": True,
            },
        }
        # Bare-name link endpoints reference workflow-level data units.
        for ref in (link.source, link.target):
            if "." not in ref:
                # Heuristic: source side → input DU; target side → output.
                # Caller can override by naming explicitly.
                if ref == link.source:
                    wf_input_dus.add(ref)
                else:
                    wf_output_dus.add(ref)

    # ---- Top-level shape -----------------------------------------------
    out: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "version": spec.version,
        # config_version: 2 is the safe-defaults regime (G7 Step 5);
        # forces auto_transfer to be honored at framework level even
        # if a future v3 ever flips it back.
        "config_version": 2,
        "steps": steps_block,
        "links": links_block,
    }
    if wf_input_dus:
        out["input_data_units"] = {name: _workflow_du_block(name) for name in sorted(wf_input_dus)}
    if wf_output_dus:
        out["output_data_units"] = {
            name: _workflow_du_block(name) for name in sorted(wf_output_dus)
        }
    if novel_python_classes:
        # Threaded through to the composer so the
        # `novel_python` fence can be reconstructed.
        out["_apecx_novel_python_by_step"] = novel_python_classes
    if sandboxed_novel_config:
        # KEPT in the persisted YAML (unlike _apecx_novel_python_by_step, which the composer pops):
        # the executor stager reads this to materialize each novel step's steps/<id>.yml file-path
        # config, then strips it before Workflow.from_config. Self-contained artifact — the executor
        # need not consult the composition record.
        out["_apecx_sandboxed_novel_config"] = sandboxed_novel_config
    return out, warnings


def _resolve_step_class(
    name: str,
    by_full: dict[str, CatalogComponent],
    by_leaf: dict[str, list[CatalogComponent]],
    warnings: list[str],
    step_id: str,
) -> CatalogComponent:
    """Map a leaf class name OR a full path to a catalog component.

    Three cases:
      1. ``name`` is already in ``by_full`` → return it (no warning).
      2. ``name`` is a leaf and exactly one catalog component has
         that leaf → return it, warn that we resolved by leaf so
         a reviewer can see what we did.
      3. Ambiguous or no match → raise ``SpecExpansionError``.
    """
    if name in by_full:
        return by_full[name]
    leaf = name.rsplit(".", 1)[-1] if "." in name else name
    candidates = by_leaf.get(leaf, [])
    if len(candidates) == 1:
        warnings.append(
            f"step {step_id!r}: resolved leaf class name {leaf!r} "
            f"to catalog entry {candidates[0].class_path!r}."
        )
        return candidates[0]
    if len(candidates) >= 2:
        listed = "; ".join(c.class_path for c in candidates)
        raise SpecExpansionError(
            f"step {step_id!r}: class_name {leaf!r} is ambiguous; "
            f"matches multiple catalog entries: {listed}. Use the full "
            "dotted path to disambiguate."
        )
    raise SpecExpansionError(
        f"step {step_id!r}: class_name {name!r} has no catalog match. "
        "Pick from the catalog's leaf names or pass a full dotted path."
    )


def _make_link_id(source: str, target: str, idx: int) -> str:
    """Stable, readable link id derived from source/target.

    Avoids collisions across multiple links with the same endpoints
    (rare but possible in fan-in graphs) by appending the source's
    index when needed."""
    safe_source = source.replace(".", "_")
    safe_target = target.replace(".", "_")
    return f"{safe_source}_to_{safe_target}_{idx}"


def _workflow_du_block(name: str) -> dict[str, Any]:
    """The shape of a workflow-level DataUnit block.

    DataUnitMemory is the universal default — fits the composer's
    typical use case (queries / responses pass as in-memory blobs).
    Operators who need persistent / streaming can override post-
    expansion. Inline dict config is legal here per the framework
    rule (DataUnit subclasses are inline-eligible).
    """
    return {
        "class": "nanobrain.core.data_unit.DataUnitMemory",
        "name": name,
        "persistent": False,
    }


__all__ = [
    "MinimalWorkflowSpec",
    "SpecExpansionError",
    "WorkflowLinkSpec",
    "WorkflowStepSpec",
    "expand_spec",
]
