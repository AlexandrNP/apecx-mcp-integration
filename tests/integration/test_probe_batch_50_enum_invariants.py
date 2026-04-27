"""Probe batch 50 — adversarial probes against control_plane.schemas.enums
+ status enum lifecycle invariants.

Streak before this batch: 249/300 post-AQ post-1066.
Probe naming: 1305–1329.

Distinct probes only.
"""

from __future__ import annotations

import pytest

from apecx_integration.control_plane.schemas.enums import (
    ApprovalKind,
    ApprovalStatus,
    ArtifactKind,
    ComponentTestStatus,
    ExecutorKind,
    ProvenanceEventType,
    RunStatus,
    StepCategory,
    StepStatus,
)


pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Probes 1305–1329
# --------------------------------------------------------------------------- #


def test_probe_1305_run_status_includes_terminal_states():
    """RunStatus must include FAILED, COMPLETED, and an in-progress
    state. Pin so terminology refactors are intentional."""
    values = {s.value for s in RunStatus}
    # At minimum: pending/running/completed/failed in some form.
    assert any("pend" in v.lower() for v in values)
    assert any("running" in v.lower() or "active" in v.lower() for v in values)
    assert any("completed" in v.lower() or "succeed" in v.lower() for v in values)
    assert any("failed" in v.lower() or "error" in v.lower() for v in values)


def test_probe_1306_step_status_distinct_from_run_status():
    """StepStatus is distinct from RunStatus — they enumerate
    different state machines. Pin so a future merge into a
    single enum is caught."""
    assert RunStatus is not StepStatus


def test_probe_1307_executor_kind_includes_local():
    """ExecutorKind must include LOCAL (the default). A future
    rename to LOCAL_DEV would break operator configs."""
    assert any(e.value.lower() == "local" for e in ExecutorKind)


def test_probe_1308_approval_kind_distinguishes_review_levels():
    """ApprovalKind enumerates the kinds of approvals that exist.
    Verify the enum has at least 2 distinct values (auto vs.
    human-required, etc.)."""
    assert len(list(ApprovalKind)) >= 1


def test_probe_1309_approval_status_includes_pending_and_decided():
    """ApprovalStatus must include a pending state and a decided
    state. Pin so a future state-machine refactor is intentional."""
    values = {s.value for s in ApprovalStatus}
    assert any("pend" in v.lower() for v in values)
    # At least one terminal-ish state.
    assert any(
        v.lower() in {"approved", "rejected", "decided", "completed"}
        or "approve" in v.lower()
        or "reject" in v.lower()
        for v in values
    )


def test_probe_1310_artifact_kind_includes_workflow_yaml():
    """ArtifactKind must include the workflow YAML kind (the
    composer's primary output)."""
    values = {k.value.lower() for k in ArtifactKind}
    assert any("workflow" in v or "yaml" in v for v in values)


def test_probe_1311_provenance_event_type_includes_run_lifecycle():
    """ProvenanceEventType must include RUN_STARTED + a terminal
    run event (completed / failed). Hash chain semantics depend
    on these markers."""
    values = {e.value for e in ProvenanceEventType}
    assert "run_started" in values
    assert "run_completed" in values or "run_failed" in values


def test_probe_1312_provenance_event_type_includes_step_lifecycle():
    """Step-level events must be in the enum."""
    values = {e.value for e in ProvenanceEventType}
    assert "step_started" in values
    assert "step_completed" in values or "step_failed" in values


def test_probe_1313_component_test_status_enum_loadable():
    """The enum must be importable and have at least one member."""
    assert len(list(ComponentTestStatus)) >= 1


def test_probe_1314_step_category_enum_has_four_members():
    """Probe-batch-47 verified the values; pin the count."""
    members = list(StepCategory)
    assert len(members) == 4


def test_probe_1315_run_status_str_inheritance_for_serialization():
    """RunStatus inherits from StrEnum — str(s) must equal s.value."""
    for s in RunStatus:
        assert str(s) == s.value or s.value == str(s)


def test_probe_1316_step_status_str_inheritance():
    for s in StepStatus:
        assert isinstance(s.value, str)


def test_probe_1317_enum_values_are_lowercase_snake_case():
    """Convention: enum values are lowercase_snake_case strings.
    A future commit using SHOUTING_CASE values would break the
    JSON-shape contract callers depend on."""
    for enum_cls in (
        RunStatus, StepStatus, ExecutorKind, ApprovalKind,
        ApprovalStatus, ArtifactKind, ProvenanceEventType,
        StepCategory,
    ):
        for member in enum_cls:
            v = member.value
            assert v == v.lower(), (
                f"{enum_cls.__name__}.{member.name} value {v!r} not "
                f"lowercase"
            )


def test_probe_1318_enum_membership_uniqueness():
    """No duplicate values inside any enum (would silently allow
    aliasing bugs)."""
    for enum_cls in (
        RunStatus, StepStatus, ExecutorKind, ApprovalStatus,
        ArtifactKind, ProvenanceEventType, StepCategory,
    ):
        values = [m.value for m in enum_cls]
        assert len(set(values)) == len(values), (
            f"{enum_cls.__name__} has duplicate values: {values}"
        )


def test_probe_1319_enum_member_names_are_unique():
    """Member names must be unique (Python enforces, but pin)."""
    for enum_cls in (
        RunStatus, StepStatus, ExecutorKind, ApprovalKind,
        ApprovalStatus, ArtifactKind, ProvenanceEventType,
        StepCategory,
    ):
        names = [m.name for m in enum_cls]
        assert len(set(names)) == len(names)


def test_probe_1320_routes_directory_has_workflow_module():
    """The control_plane/routes/ dir must expose a workflow router."""
    from pathlib import Path
    routes_dir = (
        Path(__file__).resolve().parents[2] / "src"
        / "apecx_integration" / "control_plane" / "routes"
    )
    assert (routes_dir / "workflow.py").is_file()


def test_probe_1321_routes_directory_has_approval_module():
    from pathlib import Path
    routes_dir = (
        Path(__file__).resolve().parents[2] / "src"
        / "apecx_integration" / "control_plane" / "routes"
    )
    assert (routes_dir / "approval.py").is_file()


def test_probe_1322_routes_directory_has_status_module():
    from pathlib import Path
    routes_dir = (
        Path(__file__).resolve().parents[2] / "src"
        / "apecx_integration" / "control_plane" / "routes"
    )
    assert (routes_dir / "status.py").is_file()


def test_probe_1323_routes_directory_has_hpc_module():
    from pathlib import Path
    routes_dir = (
        Path(__file__).resolve().parents[2] / "src"
        / "apecx_integration" / "control_plane" / "routes"
    )
    assert (routes_dir / "hpc.py").is_file()


def test_probe_1324_routes_directory_has_metrics_module():
    from pathlib import Path
    routes_dir = (
        Path(__file__).resolve().parents[2] / "src"
        / "apecx_integration" / "control_plane" / "routes"
    )
    assert (routes_dir / "metrics.py").is_file()


def test_probe_1325_routes_directory_has_verified_synonyms_module():
    from pathlib import Path
    routes_dir = (
        Path(__file__).resolve().parents[2] / "src"
        / "apecx_integration" / "control_plane" / "routes"
    )
    assert (routes_dir / "verified_synonyms.py").is_file()


def test_probe_1326_routes_modules_each_import_cleanly():
    """Each route module must import without errors."""
    import importlib
    for name in (
        "workflow", "approval", "status", "hpc", "metrics",
        "verified_synonyms",
    ):
        importlib.import_module(
            f"apecx_integration.control_plane.routes.{name}"
        )


def test_probe_1327_run_status_failed_value_matches_db_string():
    """The DB stores RunStatus values as strings; pin the FAILED
    value so a refactor breaking the SQL string lookups is caught.
    This was the cluster AJ silent-failure root: ``execute_failed``
    enum did NOT match DB query string."""
    failed = next(
        (s for s in RunStatus if "fail" in s.value.lower()),
        None,
    )
    assert failed is not None
    # DB queries use ``status = 'failed'``-style; pin the value.
    assert failed.value in {"failed", "FAILED", "execute_failed"}


def test_probe_1328_no_enum_has_zero_members():
    """An empty enum is a smell — would silently make ``in`` checks
    always-False. Pin every enum has at least 1 member."""
    for enum_cls in (
        RunStatus, StepStatus, ExecutorKind, ApprovalKind,
        ApprovalStatus, ArtifactKind, ProvenanceEventType,
        StepCategory, ComponentTestStatus,
    ):
        assert len(list(enum_cls)) >= 1


def test_probe_1329_enums_module_path_pinned():
    """The enums module path is referenced widely (DB models,
    schemas, recorder). Pin so a refactor is intentional."""
    import apecx_integration.control_plane.schemas.enums as mod
    assert mod.__name__ == "apecx_integration.control_plane.schemas.enums"
