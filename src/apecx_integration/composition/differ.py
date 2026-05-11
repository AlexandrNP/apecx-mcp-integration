"""T06 categorization — what's composed vs. what's novel in a generated workflow.

AP §5.6 defines four categories that matter to a reviewer:

- ``composed_standard``   — library component used unchanged. The step's
                            ``class`` matches a retrieved catalog
                            component AND the step's ``config`` points
                            at the catalog's canonical wrapper YAML.
- ``composed_parameterized`` — library component with an inline
                            ``config`` mapping (not the canonical
                            wrapper path). The class is library; the
                            wiring is bespoke.
- ``composed_wrapped``    — library component whose ``config`` references
                            a novel-Python step_id. The class is library
                            but some novel Python is stitched in.
- ``novel``               — step_id appears in the ``novel_python`` block,
                            OR the class path doesn't match any
                            retrieved component AND cannot be imported
                            as a known Step subclass.

The categorization is explicit about being heuristic (AP §5.6 Risks).
Reviewers who disagree can always open the YAML; this tier exists to
route attention, not to be a final verdict.

Retrieval-gap second pass (A2, 2026-05-11)
------------------------------------------
The original heuristic — "class is library iff its path is in the
retrieved-components set" — over-flags real library components when
retrieval recall is imperfect (e.g. FAISS top-k=10 substring-match
missed a known component). Concrete failure shape from the
Automated_Workflow_Generation_Issues.md report: ``SynthesisContextAssemblyStep``
labeled NOVEL/orphan despite existing on disk at
``composition/steps/synthesis_context_assembly_step.py``.

The second pass closes the gap without re-architecting retrieval:
when a class path is NOT in the retrieved set, try to import it
and check whether it's a ``BaseStep`` subclass. Three outcomes:

- imports + Step subclass → ``COMPOSED_PARAMETERIZED`` with
  ``retrieval_gap=True`` on the categorization (the reviewer still
  sees that retrieval missed it — that's a retrieval-recall signal,
  not a categorizer bug).
- imports + NOT Step subclass → ``NOVEL`` with reason
  "class exists but is not a Step subclass — bug, not orphan".
- import fails → ``NOVEL`` with reason "unresolvable class path —
  likely typo / hallucinated".

Each outcome carries a distinct ``reason`` string so a reviewer
glancing at the diff knows whether to fix retrieval, fix the
class hierarchy, or correct an LLM hallucination.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class StepCategory(StrEnum):
    """AP §5.6 step categorization.

    Values are lowercase strings so they serialize naturally into YAML /
    JSON payloads without needing a custom encoder.
    """

    COMPOSED_STANDARD = "composed_standard"
    COMPOSED_PARAMETERIZED = "composed_parameterized"
    COMPOSED_WRAPPED = "composed_wrapped"
    NOVEL = "novel"


@dataclass(frozen=True, kw_only=True)
class StepCategorization:
    """One row in the diff summary — per step in the workflow.

    ``retrieval_gap`` (A2, 2026-05-11) is True when this step was
    classified as a library component via the disk-existence /
    import-resolves second pass — i.e., it IS a Step subclass on disk
    but missed the retrieval top-k. Reviewers reading the diff
    should know that retrieval recall is imperfect, not that the
    categorizer changed its rules. Operators tracking retrieval
    quality count occurrences of ``retrieval_gap=True`` as the
    regression metric for retrieval-side improvements (B3).
    """

    step_id: str
    step_class: str
    category: StepCategory
    reason: str  # one short sentence explaining the verdict
    retrieval_gap: bool = False


@dataclass(frozen=True, kw_only=True)
class CategorizedWorkflow:
    """The diff-UX payload for a composed workflow."""

    categorizations: tuple[StepCategorization, ...]

    @property
    def summary_sentence(self) -> str:
        """AP §5.6 human-readable summary.

        Format: "This workflow has N steps. M compose library components
        (K standard + L parameterized + P wrapped). Q steps are novel
        Python requiring review."

        When any step carries ``retrieval_gap=True`` (A2, 2026-05-11),
        a second sentence is appended noting how many: "Note: G of
        those library components were found via disk-import fallback
        because retrieval missed them — review retrieval recall."
        Chosen over a table format because reviewers scanning a Slack
        notification / MCP response see the at-a-glance counts first;
        the per-step detail lives in the categorizations tuple for
        UIs that want it.
        """
        n = len(self.categorizations)
        std = self.count(StepCategory.COMPOSED_STANDARD)
        par = self.count(StepCategory.COMPOSED_PARAMETERIZED)
        wrp = self.count(StepCategory.COMPOSED_WRAPPED)
        nov = self.count(StepCategory.NOVEL)
        composed = std + par + wrp
        retrieval_gaps = sum(1 for c in self.categorizations if c.retrieval_gap)
        base = (
            f"This workflow has {n} step(s). {composed} compose library "
            f"components ({std} standard + {par} parameterized + "
            f"{wrp} wrapped). {nov} step(s) are novel Python requiring "
            f"review."
        )
        if retrieval_gaps:
            base += (
                f" Note: {retrieval_gaps} of those library components "
                "were classified via disk-import fallback because the "
                "retrieval top-k missed them — review retrieval recall."
            )
        return base

    def count(self, category: StepCategory) -> int:
        return sum(1 for c in self.categorizations if c.category is category)

    def novel_step_ids(self) -> tuple[str, ...]:
        return tuple(c.step_id for c in self.categorizations if c.category is StepCategory.NOVEL)


def categorize_workflow(
    *,
    workflow_dict: dict[str, Any],
    novel_python: dict[str, str],
    retrieved_class_paths: set[str],
    catalog_yaml_paths: dict[str, str] | None = None,
) -> CategorizedWorkflow:
    """Run the AP §5.6 categorization heuristic.

    Args:
        workflow_dict: the parsed composed YAML (top-level mapping with
            ``steps:`` block).
        novel_python: step_id → source code (from the composer's parsed
            ``novel_python`` fence). Any step_id listed here is novel
            regardless of its class path.
        retrieved_class_paths: the set of class paths the composer
            pulled from the catalog. A step whose class is in this set
            is a candidate for "composed_*".
        catalog_yaml_paths: optional mapping from class_path → canonical
            wrapper YAML path. When provided, enables distinguishing
            ``composed_standard`` from ``composed_parameterized`` by
            checking whether the step's ``config`` equals the canonical
            path. When None, the heuristic can only tell ``composed_*``
            (generic) from ``novel`` — degrading gracefully.

    Returns:
        CategorizedWorkflow with one entry per step in workflow_dict.
    """
    steps = workflow_dict.get("steps") or {}
    if not isinstance(steps, dict):
        raise ValueError(f"workflow 'steps' must be a mapping; got {type(steps).__name__}")

    categorizations: list[StepCategorization] = []
    novel_ids = set(novel_python.keys())
    yaml_paths = catalog_yaml_paths or {}

    for step_id, step_body in steps.items():
        step_class = ""
        step_config: Any = None
        if isinstance(step_body, dict):
            step_class = str(step_body.get("class") or "")
            step_config = step_body.get("config")

        category, reason, retrieval_gap = _classify_step(
            step_id=str(step_id),
            step_class=step_class,
            step_config=step_config,
            novel_ids=novel_ids,
            retrieved_class_paths=retrieved_class_paths,
            yaml_paths=yaml_paths,
        )
        categorizations.append(
            StepCategorization(
                step_id=str(step_id),
                step_class=step_class,
                category=category,
                reason=reason,
                retrieval_gap=retrieval_gap,
            )
        )

    return CategorizedWorkflow(categorizations=tuple(categorizations))


def _classify_step(
    *,
    step_id: str,
    step_class: str,
    step_config: Any,
    novel_ids: set[str],
    retrieved_class_paths: set[str],
    yaml_paths: dict[str, str],
) -> tuple[StepCategory, str, bool]:
    """Return ``(category, reason, retrieval_gap)`` for one step.

    ``retrieval_gap`` is True iff the class was rescued by the
    disk-existence + Step-subclass second pass — i.e., it IS library
    but missed retrieval. Callers surface this through
    ``StepCategorization.retrieval_gap`` so the reviewer can
    distinguish "categorizer happy path" from "categorizer fallback".
    """
    if step_id in novel_ids:
        return (
            StepCategory.NOVEL,
            "step_id appears in the novel_python fence.",
            False,
        )
    if not step_class:
        return (
            StepCategory.NOVEL,
            "step body has no `class:` field — orphan.",
            False,
        )

    in_retrieval = step_class in retrieved_class_paths
    retrieval_gap = False
    if not in_retrieval:
        # A2 second pass: was this class missed by retrieval (recall
        # gap) or is it a true unknown (typo / hallucination /
        # non-Step class)? Import + subclass check is the cheap,
        # framework-native way to tell.
        outcome = _import_outcome(step_class)
        if outcome == "step":
            retrieval_gap = True
            # Fall through to the library-class classification below.
        elif outcome == "import_failed":
            return (
                StepCategory.NOVEL,
                "class path could not be imported — likely typo or "
                "hallucinated; not retrieval-recall.",
                False,
            )
        elif outcome == "not_step":
            return (
                StepCategory.NOVEL,
                "class exists but is not a subclass of "
                "`nanobrain.core.step.BaseStep` — bug in the workflow, "
                "not an orphan.",
                False,
            )
        else:
            # Framework missing or some other classification failure.
            # Conservative: keep the legacy behavior.
            return (
                StepCategory.NOVEL,
                "step class does not match any retrieved library component — orphan.",
                False,
            )

    # The class is library. Distinguish standard / parameterized / wrapped
    # by the shape of ``config``.
    if _config_references_novel(step_config, novel_ids):
        return (
            StepCategory.COMPOSED_WRAPPED,
            "library class with config that references a novel-Python step — wrapping required.",
            retrieval_gap,
        )
    if isinstance(step_config, str):
        canonical = yaml_paths.get(step_class)
        if canonical is not None and step_config == canonical:
            return (
                StepCategory.COMPOSED_STANDARD,
                "library class with canonical wrapper YAML path."
                if not retrieval_gap
                else (
                    "library class (recovered via disk-import "
                    "fallback) with the catalog's canonical wrapper "
                    "path — review retrieval recall."
                ),
                retrieval_gap,
            )
        return (
            StepCategory.COMPOSED_PARAMETERIZED,
            "library class with a non-canonical config path."
            if not retrieval_gap
            else (
                "library class (recovered via disk-import fallback) "
                "with a non-canonical config path — review retrieval "
                "recall."
            ),
            retrieval_gap,
        )
    if isinstance(step_config, dict):
        return (
            StepCategory.COMPOSED_PARAMETERIZED,
            "library class with an inline config mapping."
            if not retrieval_gap
            else (
                "library class (recovered via disk-import fallback) "
                "with an inline config mapping — review retrieval "
                "recall."
            ),
            retrieval_gap,
        )
    return (
        StepCategory.COMPOSED_PARAMETERIZED,
        "library class with missing or unrecognized config shape."
        if not retrieval_gap
        else (
            "library class (recovered via disk-import fallback) with "
            "missing or unrecognized config shape — review retrieval "
            "recall."
        ),
        retrieval_gap,
    )


def _import_outcome(class_path: str) -> str:
    """Classify what happens when we try to import ``class_path``.

    Returns one of: ``"step"`` (imports + Step subclass),
    ``"not_step"`` (imports but not a Step subclass), ``"import_failed"``
    (module or attribute missing), or ``"framework_missing"`` (the
    nanobrain core itself is unimportable — only happens in
    tests-without-deps environments).

    Pure helper: no side effects beyond importing the module under
    inspection, which Python caches in ``sys.modules`` regardless of
    who calls importlib.import_module. We deliberately do NOT cache
    the verdict here — the cache would hide a class-path that was
    valid earlier in the process but became invalid (e.g. someone
    deleted the file mid-run). The legitimate use case is one-shot
    per compose() call; the import cache eats most of the cost.
    """
    try:
        from nanobrain.core.step import BaseStep
    except ImportError as exc:
        log.warning(
            "Disk-import second pass disabled: nanobrain.core.step "
            "unimportable (%s); falling back to retrieval-only "
            "categorization.",
            exc,
        )
        return "framework_missing"

    if not class_path or "." not in class_path:
        return "import_failed"
    module_path, _, class_name = class_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except Exception:  # ImportError, ValueError, ModuleNotFoundError, etc.
        return "import_failed"
    target = getattr(module, class_name, None)
    if target is None or not isinstance(target, type):
        return "import_failed"
    try:
        if issubclass(target, BaseStep):
            return "step"
    except TypeError:
        return "import_failed"
    return "not_step"


def _config_references_novel(config: Any, novel_ids: set[str]) -> bool:
    """Does ``config`` mention any novel step_id?

    The LLM can wrap a library class by passing a novel-Python step as
    one of its config fields (e.g. ``{ preprocessor: "rogue_extractor" }``
    where ``rogue_extractor`` is a novel step). This walks the config
    recursively looking for any string value or key that matches a
    novel step_id.
    """
    if not novel_ids:
        return False
    if isinstance(config, str):
        return config in novel_ids
    if isinstance(config, dict):
        for k, v in config.items():
            if isinstance(k, str) and k in novel_ids:
                return True
            if _config_references_novel(v, novel_ids):
                return True
    if isinstance(config, list):
        return any(_config_references_novel(item, novel_ids) for item in config)
    return False


__all__ = [
    "CategorizedWorkflow",
    "StepCategorization",
    "StepCategory",
    "categorize_workflow",
]
