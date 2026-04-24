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
                            retrieved component.

The categorization is explicit about being heuristic (AP §5.6 Risks).
Reviewers who disagree can always open the YAML; this tier exists to
route attention, not to be a final verdict.

Brutal-truth caveat
-------------------
The LLM can emit a ``class`` path that *claims* to be a library
component but mis-spells it or points at a class that no longer
exists. This categorizer trusts the retrieved-components list: if the
exact class path isn't in retrieval, the step falls to ``novel``
(orphan), which is the correct failure mode — the reviewer sees
"novel, needs review" instead of a false-positive "composed_standard".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StepCategory(str, Enum):
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
    """One row in the diff summary — per step in the workflow."""

    step_id: str
    step_class: str
    category: StepCategory
    reason: str  # one short sentence explaining the verdict


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
        return (
            f"This workflow has {n} step(s). {composed} compose library "
            f"components ({std} standard + {par} parameterized + "
            f"{wrp} wrapped). {nov} step(s) are novel Python requiring "
            f"review."
        )

    def count(self, category: StepCategory) -> int:
        return sum(1 for c in self.categorizations if c.category is category)

    def novel_step_ids(self) -> tuple[str, ...]:
        return tuple(
            c.step_id
            for c in self.categorizations
            if c.category is StepCategory.NOVEL
        )


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
        raise ValueError(
            f"workflow 'steps' must be a mapping; got {type(steps).__name__}"
        )

    categorizations: list[StepCategorization] = []
    novel_ids = set(novel_python.keys())
    yaml_paths = catalog_yaml_paths or {}

    for step_id, step_body in steps.items():
        step_class = ""
        step_config: Any = None
        if isinstance(step_body, dict):
            step_class = str(step_body.get("class") or "")
            step_config = step_body.get("config")

        category, reason = _classify_step(
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
) -> tuple[StepCategory, str]:
    if step_id in novel_ids:
        return (
            StepCategory.NOVEL,
            "step_id appears in the novel_python fence.",
        )
    if not step_class or step_class not in retrieved_class_paths:
        return (
            StepCategory.NOVEL,
            "step class does not match any retrieved library component — "
            "orphan.",
        )
    # The class is library. Distinguish standard / parameterized / wrapped
    # by the shape of ``config``.
    if _config_references_novel(step_config, novel_ids):
        return (
            StepCategory.COMPOSED_WRAPPED,
            "library class with config that references a novel-Python "
            "step — wrapping required.",
        )
    if isinstance(step_config, str):
        canonical = yaml_paths.get(step_class)
        if canonical is not None and step_config == canonical:
            return (
                StepCategory.COMPOSED_STANDARD,
                "library class with canonical wrapper YAML path.",
            )
        # String config that's not the canonical path — either a
        # bespoke wrapper YAML or a path typo. Treat as parameterized:
        # the reviewer should confirm the file exists and is correct.
        return (
            StepCategory.COMPOSED_PARAMETERIZED,
            "library class with a non-canonical config path.",
        )
    if isinstance(step_config, dict):
        return (
            StepCategory.COMPOSED_PARAMETERIZED,
            "library class with an inline config mapping.",
        )
    # Missing / null config on a library class — treat as parameterized,
    # since the downstream from_config will complain anyway.
    return (
        StepCategory.COMPOSED_PARAMETERIZED,
        "library class with missing or unrecognized config shape.",
    )


def _config_references_novel(
    config: Any, novel_ids: set[str]
) -> bool:
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
