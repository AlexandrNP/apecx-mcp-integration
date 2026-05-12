"""Regression-metric queries over the control-plane DB.

This session shipped three measurable signals that operators should
track over time so the project's reliability improvements stay
reliable:

  - ``compose_retries`` (C1) — distribution of how many retry
    rounds the composer needed per workflow. Sustained non-zero
    means the LLM is emitting framework-illegal workflows on the
    first try; the prompt + retrieval may need tightening.
  - ``runtime_violations`` (C2) — count + rule_id breakdown of
    framework errors that A1 did NOT catch at compose-time. Any
    nonzero count is a coverage gap to investigate.
  - ``retrieval_gap`` (A2) — fraction of steps classified via the
    disk-import fallback. High rate = retrieval recall problem.

Why a programmatic API + a CLI: the operator path is one-shot
("how am I doing right now?"); the regression-test path needs the
counts as data structures to assert on. Splitting the surface keeps
both paths first-class.

Framework-native: uses the existing SQLAlchemy session factory and
``GeneratedArtifact`` ORM model. No raw SQL — joins and JSON-shape
parsing happen in Python so a future Postgres / SQLite migration
doesn't break the queries.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apecx_integration.control_plane.models.entities import (
    GeneratedArtifact as GeneratedArtifactORM,
)

# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ComposeRetryStats:
    """C1 (compose_retries) distribution.

    ``distribution[N]`` is the number of artifacts whose
    composition required exactly N retry rounds. ``artifacts_with_retries``
    counts artifacts where N > 0 — the headline number an operator
    watches.
    """

    distribution: dict[int, int] = field(default_factory=dict)
    artifacts_with_retries: int = 0
    total_artifacts: int = 0

    @property
    def retry_rate(self) -> float:
        if self.total_artifacts == 0:
            return 0.0
        return self.artifacts_with_retries / self.total_artifacts


@dataclass(frozen=True, kw_only=True)
class RuntimeViolationStats:
    """C2 (runtime_violations) breakdown.

    Each row in ``GeneratedArtifact.composition_summary
    ['runtime_violations']`` is one structured violation record
    written by ``LocalExecutor._record_runtime_violation``. We
    tally them by ``rule_id`` so an operator can spot whether the
    A1 coverage gap is dominated by a single failure shape.
    """

    total_violations: int = 0
    artifacts_with_violations: int = 0
    by_rule_id: dict[str, int] = field(default_factory=dict)
    by_failure_class: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RetrievalGapStats:
    """A2 (retrieval_gap) rate.

    Counts steps across all artifacts; ``rate`` is the fraction of
    steps that needed A2's disk-import fallback to be correctly
    classified. A sustained nonzero rate signals retrieval recall
    work (B3 follow-ups).
    """

    total_steps: int = 0
    retrieval_gap_steps: int = 0

    @property
    def rate(self) -> float:
        if self.total_steps == 0:
            return 0.0
        return self.retrieval_gap_steps / self.total_steps


@dataclass(frozen=True, kw_only=True)
class ClassPathRepairStats:
    """CPR (2026-05-11) auto-repair frequency.

    Each repair is one LLM hallucination of the suffix-drop shape
    that the catalog-grounded resolver fixed automatically. A
    sustained high rate is good news (the resolver is earning its
    keep) AND bad news (the LLM keeps making the same mistake — B1
    prompt work could try to reduce it at the source).
    """

    total_repairs: int = 0
    artifacts_with_repairs: int = 0


@dataclass(frozen=True, kw_only=True)
class RegressionMetricsReport:
    """The full picture an operator wants at a glance."""

    total_artifacts: int
    compose_retries: ComposeRetryStats
    runtime_violations: RuntimeViolationStats
    retrieval_gap: RetrievalGapStats
    class_path_repairs: ClassPathRepairStats
    since: datetime | None = None
    computed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict for the CLI's ``--json`` output."""
        d = asdict(self)
        # asdict returns datetimes verbatim; ISO-format them.
        if self.since is not None:
            d["since"] = self.since.isoformat()
        d["computed_at"] = self.computed_at.isoformat()
        # Computed properties aren't included by asdict — add them.
        d["compose_retries"]["retry_rate"] = self.compose_retries.retry_rate
        d["retrieval_gap"]["rate"] = self.retrieval_gap.rate
        return d

    def to_table(self) -> str:
        """Human-readable text. Stable column widths so eyeballs
        can compare two consecutive reports without surprises.
        """
        lines: list[str] = []
        lines.append(
            f"Regression metrics — {self.total_artifacts} generated artifact(s)"
            + (f" since {self.since.isoformat()}" if self.since else "")
        )
        lines.append("=" * 78)
        # C1 — compose_retries
        cr = self.compose_retries
        lines.append("")
        lines.append(
            f"Compose retries (C1): "
            f"{cr.artifacts_with_retries}/{cr.total_artifacts} "
            f"artifacts ({cr.retry_rate:.1%}) needed >= 1 retry"
        )
        if cr.distribution:
            lines.append("  Distribution:")
            for n in sorted(cr.distribution):
                lines.append(f"    retries={n}: {cr.distribution[n]} artifact(s)")
        # C2 — runtime_violations
        rv = self.runtime_violations
        lines.append("")
        lines.append(
            f"Runtime violations (C2): {rv.total_violations} total "
            f"across {rv.artifacts_with_violations} artifact(s)"
        )
        if rv.by_rule_id:
            lines.append("  By rule_id:")
            for rule_id in sorted(rv.by_rule_id, key=lambda k: -rv.by_rule_id[k]):
                lines.append(f"    {rule_id}: {rv.by_rule_id[rule_id]}")
        if rv.by_failure_class:
            lines.append("  By failure_class:")
            for failure_class in sorted(rv.by_failure_class):
                lines.append(f"    {failure_class}: {rv.by_failure_class[failure_class]}")
        # A2 — retrieval_gap
        rg = self.retrieval_gap
        lines.append("")
        lines.append(
            f"Retrieval gap (A2): "
            f"{rg.retrieval_gap_steps}/{rg.total_steps} step(s) "
            f"({rg.rate:.1%}) classified via disk-import fallback"
        )
        # CPR — class_path_repairs
        cpr = self.class_path_repairs
        lines.append("")
        lines.append(
            f"Class-path repairs (CPR): {cpr.total_repairs} auto-corrected "
            f"across {cpr.artifacts_with_repairs} artifact(s) "
            "(LLM hallucinated suffix-drop on class paths)"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def compute_regression_metrics(
    session_factory: sessionmaker[Session],
    *,
    since: datetime | None = None,
) -> RegressionMetricsReport:
    """Compute all three regression metrics from one DB pass.

    Args:
        session_factory: a ready-to-use session factory; we open
            ONE session for the duration of the read.
        since: optional UTC datetime — only artifacts whose Artifact
            row's ``created_at`` is >= ``since`` are counted. ``None``
            means all-time (the operator-default for first runs).

    Returns:
        A ``RegressionMetricsReport`` ready for ``.to_dict()`` /
        ``.to_table()``. The function does NOT print or log — those
        are CLI concerns.
    """
    cr_dist: dict[int, int] = {}
    cr_with_retries = 0
    rv_total = 0
    rv_artifacts = 0
    rv_by_rule: dict[str, int] = {}
    rv_by_class: dict[str, int] = {}
    rg_total_steps = 0
    rg_gap_steps = 0
    cpr_total = 0
    cpr_artifacts = 0
    total_artifacts = 0

    with session_factory() as session:
        rows = _select_artifacts(session, since=since)
        for ga in rows:
            total_artifacts += 1
            summary = ga.composition_summary or {}

            # C1 — compose_retries
            retries_raw = summary.get("compose_retries", 0)
            retries = int(retries_raw) if isinstance(retries_raw, (int, float)) else 0
            cr_dist[retries] = cr_dist.get(retries, 0) + 1
            if retries > 0:
                cr_with_retries += 1

            # C2 — runtime_violations
            violations = summary.get("runtime_violations") or []
            if isinstance(violations, list) and violations:
                rv_artifacts += 1
                for entry in violations:
                    if not isinstance(entry, dict):
                        continue
                    rv_total += 1
                    rule_id = str(entry.get("rule_id") or "runtime_other")
                    rv_by_rule[rule_id] = rv_by_rule.get(rule_id, 0) + 1
                    failure_class = str(entry.get("failure_class") or "unknown")
                    rv_by_class[failure_class] = rv_by_class.get(failure_class, 0) + 1

            # A2 — retrieval_gap
            categorizations = summary.get("step_categorizations") or []
            if isinstance(categorizations, list):
                for cat in categorizations:
                    if not isinstance(cat, dict):
                        continue
                    rg_total_steps += 1
                    if cat.get("retrieval_gap") is True:
                        rg_gap_steps += 1

            # CPR — class_path_repairs
            cpr_list = summary.get("class_path_repairs") or []
            if isinstance(cpr_list, list) and cpr_list:
                cpr_artifacts += 1
                cpr_total += sum(1 for c in cpr_list if isinstance(c, dict))

    return RegressionMetricsReport(
        total_artifacts=total_artifacts,
        compose_retries=ComposeRetryStats(
            distribution=cr_dist,
            artifacts_with_retries=cr_with_retries,
            total_artifacts=total_artifacts,
        ),
        runtime_violations=RuntimeViolationStats(
            total_violations=rv_total,
            artifacts_with_violations=rv_artifacts,
            by_rule_id=rv_by_rule,
            by_failure_class=rv_by_class,
        ),
        retrieval_gap=RetrievalGapStats(
            total_steps=rg_total_steps,
            retrieval_gap_steps=rg_gap_steps,
        ),
        class_path_repairs=ClassPathRepairStats(
            total_repairs=cpr_total,
            artifacts_with_repairs=cpr_artifacts,
        ),
        since=since,
    )


def _select_artifacts(session: Session, *, since: datetime | None) -> list[GeneratedArtifactORM]:
    """Return every GeneratedArtifact row honoring the ``since`` filter.

    Joins to the ``Artifact`` table for ``created_at`` filtering when
    ``since`` is provided; otherwise the join is skipped to keep the
    common all-time-read path cheap on large tables.
    """
    if since is None:
        return list(session.execute(select(GeneratedArtifactORM)).scalars())
    from apecx_integration.control_plane.models.entities import (
        Artifact as ArtifactORM,
    )

    stmt = (
        select(GeneratedArtifactORM)
        .join(ArtifactORM, ArtifactORM.id == GeneratedArtifactORM.artifact_id)
        .where(ArtifactORM.created_at >= since)
    )
    return list(session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# JSON-shaped output helper (also usable from notebooks)
# ---------------------------------------------------------------------------


def report_as_json(report: RegressionMetricsReport, *, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), indent=indent, default=str)


__all__ = [
    "ClassPathRepairStats",
    "ComposeRetryStats",
    "RegressionMetricsReport",
    "RetrievalGapStats",
    "RuntimeViolationStats",
    "compute_regression_metrics",
    "report_as_json",
]
