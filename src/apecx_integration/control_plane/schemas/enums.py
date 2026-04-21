"""String-valued enums shared across Control Plane entities.

Sourced from architectural_plan.md §4 (data model). Kept in their own module to
avoid circular imports between entity schemas that reference each other's status
types.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExecutorKind(StrEnum):
    LOCAL = "local"
    GLOBUS_COMPUTE = "globus_compute"
    PBS_BUNDLE = "pbs_bundle"


class ApprovalKind(StrEnum):
    HARD = "hard"
    SOFT = "soft"
    SILENT = "silent"
    ALLOCATION = "allocation"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    APPROVED_WITH_MODIFICATIONS = "approved_with_modifications"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"
    TIMED_OUT = "timed_out"


class ArtifactKind(StrEnum):
    INPUT = "input"
    INTERMEDIATE = "intermediate"
    OUTPUT = "output"
    GENERATED_WORKFLOW = "generated_workflow"
    GENERATED_PYTHON = "generated_python"


class ProvenanceEventType(StrEnum):
    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    ARTIFACT_CREATED = "artifact_created"
    WORKFLOW_GENERATED = "workflow_generated"
    ALLOCATION_ESTIMATED = "allocation_estimated"
    ALLOCATION_CONFIRMED = "allocation_confirmed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class ComponentTestStatus(StrEnum):
    PASSING = "passing"
    FAILING = "failing"
    UNTESTED = "untested"


class StepCategory(StrEnum):
    """Disposition category from the differential-review UX (T06).

    See architectural_plan.md §5.6.
    """

    COMPOSED_STANDARD = "composed_standard"
    COMPOSED_PARAMETERIZED = "composed_parameterized"
    COMPOSED_WRAPPED = "composed_wrapped"
    NOVEL = "novel"
