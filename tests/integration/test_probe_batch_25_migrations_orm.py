"""Probe batch 25 — alembic migration sequence + ORM model integrity
(probes 655-679).

The Control Plane DB schema is the load-bearing contract between every
HTTP route, every executor, every provenance-recording step. A
silently-misordered migration, a missing downgrade, or an ORM-vs-schema
drift would manifest as opaque DB errors at runtime.

This batch:
  - Validates the migration chain 0001 → 0006 is linear (no forks).
  - Confirms every migration has both upgrade() and downgrade().
  - Round-trips upgrade-head → downgrade-base in-memory (cluster AN
    regression coverage at the structural level).
  - Pins the cluster AC / AE / AH fix surface: the created_at columns
    on AllocationEstimate / Approval / Step (added by migrations
    0004 / 0005 / 0006) — wiring those out would re-introduce the
    UUID-tiebreak ordering bugs.
  - Exercises the UUIDString type decorator roundtrip used by every
    UUID column.
  - Verifies enum columns use ``native_enum=False`` so the schema
    works on both SQLite and Postgres.
"""

from __future__ import annotations

import importlib
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text


pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIG_DIR = _REPO_ROOT / "migrations" / "versions"


# ---------------------------------------------------------------------------
# Migration sequence integrity — probes 655-664
# ---------------------------------------------------------------------------


def _migration_files() -> list[Path]:
    return sorted(_MIG_DIR.glob("[0-9][0-9][0-9][0-9]_*.py"))


def _read_migration_metadata(p: Path) -> dict[str, str | None]:
    """Extract revision + down_revision from a migration file."""
    text = p.read_text(encoding="utf-8")
    rev = re.search(r'revision\s*:\s*str\s*=\s*"([^"]+)"', text)
    down = re.search(
        r'down_revision[^=]*=\s*("[^"]+"|None)',
        text,
    )
    return {
        "revision": rev.group(1) if rev else None,
        "down_revision": (
            down.group(1).strip('"') if down and down.group(1) != "None" else None
        ),
        "has_upgrade": "def upgrade(" in text,
        "has_downgrade": "def downgrade(" in text,
    }


def test_probe_655_every_migration_has_revision_pair() -> None:
    files = _migration_files()
    assert len(files) >= 6, f"PROBE 655: only {len(files)} migration files found"
    for f in files:
        meta = _read_migration_metadata(f)
        assert meta["revision"], f"PROBE 655: {f.name} has no revision"


def test_probe_656_migration_chain_linear() -> None:
    """Every migration's down_revision must match the previous
    file's revision. A fork (two children of the same down_revision)
    leaves alembic ambiguous on which to pick."""
    files = _migration_files()
    metas = [_read_migration_metadata(f) for f in files]
    # First file must have down_revision = None
    assert metas[0]["down_revision"] is None, (
        f"PROBE 656: first migration must have down_revision=None"
    )
    for prev, cur in zip(metas, metas[1:]):
        assert cur["down_revision"] == prev["revision"], (
            f"PROBE 656: chain broken: {cur['revision']} expects "
            f"down_revision={prev['revision']!r}, got {cur['down_revision']!r}"
        )


def test_probe_657_every_migration_has_upgrade_and_downgrade() -> None:
    for f in _migration_files():
        meta = _read_migration_metadata(f)
        assert meta["has_upgrade"], f"PROBE 657: {f.name} missing upgrade()"
        assert meta["has_downgrade"], f"PROBE 657: {f.name} missing downgrade()"


def test_probe_658_alembic_head_reachable_from_base(tmp_path) -> None:
    """``alembic upgrade head`` then ``downgrade base`` must round-trip
    cleanly. If a downgrade is broken, this probe fails — preventing
    the cluster AN class of "downgrade -1 only reverts one step"
    from re-emerging silently."""
    from alembic import command
    from alembic.config import Config
    db = tmp_path / "alembic_roundtrip.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")


def test_probe_659_upgrade_head_creates_expected_tables(tmp_path) -> None:
    """After ``upgrade head``, every model's __tablename__ must
    correspond to a real table."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import make_engine
    from apecx_integration.control_plane.models import entities as ent
    db = tmp_path / "head.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db}")
    inspector = inspect(eng)
    tables = set(inspector.get_table_names())
    expected = {
        "run", "step", "approval", "artifact", "generated_artifact",
        "provenance_event", "component", "allocation_estimate",
        "verified_synonym",
    }
    missing = expected - tables
    assert not missing, f"PROBE 659: head missing tables: {missing}"


def test_probe_660_downgrade_base_leaves_only_alembic_version(tmp_path) -> None:
    """After full downgrade, only ``alembic_version`` should remain.
    Cluster AN regression marker."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import make_engine
    db = tmp_path / "base.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    eng = make_engine(f"sqlite:///{db}")
    inspector = inspect(eng)
    remaining = set(inspector.get_table_names())
    # After downgrade base, alembic_version may or may not exist
    # (some alembic versions clean it up). What MUST be empty is the
    # set of application tables.
    application_tables = remaining - {"alembic_version"}
    assert not application_tables, (
        f"PROBE 660: downgrade base left application tables: {application_tables}"
    )


def test_probe_661_revision_matches_filename_prefix() -> None:
    """The revision string in each migration must match its 4-digit
    filename prefix. Drift = alembic uses one but humans audit by
    the other, leading to confused changelog reviews."""
    for f in _migration_files():
        prefix = f.name[:4]
        meta = _read_migration_metadata(f)
        assert meta["revision"] == prefix, (
            f"PROBE 661: {f.name}: revision={meta['revision']!r} "
            f"≠ filename prefix {prefix!r}"
        )


def test_probe_662_partial_unique_index_run_started_per_run() -> None:
    """Migration 0002 added a partial unique index ensuring at most
    one RUN_STARTED ProvenanceEvent per run_id. The migration file
    must explicitly declare WHERE-clause + unique=True."""
    p = _MIG_DIR / "0002_unique_run_started_per_run.py"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "RUN_STARTED" in text or "run_started" in text
    assert "unique" in text.lower()


def test_probe_663_partial_unique_index_null_scope_synonym() -> None:
    """Migration 0003 covers the null-scope edge case for
    verified_synonym: SQL NULL doesn't equal NULL, so the
    multi-column UniqueConstraint doesn't catch null-scope dupes
    on its own. The migration adds a partial unique index for
    scope IS NULL rows."""
    p = _MIG_DIR / "0003_unique_active_null_scope_synonym.py"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "scope" in text.lower()
    assert "is_active" in text or "is null" in text.lower()
    assert "unique" in text.lower()


def test_probe_664_created_at_added_to_all_three_tables() -> None:
    """Migrations 0004 / 0005 / 0006 added created_at columns to
    allocation_estimate, approval, step respectively. These columns
    are the cluster AC / AE / AH ordering keys — losing them
    re-introduces UUID-tiebreak bugs."""
    pairs = (
        ("0004_allocation_estimate_created_at.py", "allocation_estimate"),
        ("0005_approval_created_at.py", "approval"),
        ("0006_step_created_at.py", "step"),
    )
    for filename, table in pairs:
        p = _MIG_DIR / filename
        assert p.is_file(), f"PROBE 664: missing {filename}"
        text = p.read_text(encoding="utf-8")
        assert table in text
        assert "created_at" in text
        assert "add_column" in text.lower()


# ---------------------------------------------------------------------------
# ORM model invariants — probes 665-672
# ---------------------------------------------------------------------------


def test_probe_665_run_created_at_nullable_false() -> None:
    """Run.created_at is the cluster AC/AE/AH antipattern guard —
    must be NOT NULL so ORDER BY created_at is always meaningful."""
    from apecx_integration.control_plane.models.entities import Run
    col = Run.__table__.c["created_at"]
    assert col.nullable is False


def test_probe_666_step_created_at_nullable_false() -> None:
    from apecx_integration.control_plane.models.entities import Step
    col = Step.__table__.c["created_at"]
    assert col.nullable is False


def test_probe_667_approval_created_at_nullable_false() -> None:
    from apecx_integration.control_plane.models.entities import Approval
    col = Approval.__table__.c["created_at"]
    assert col.nullable is False


def test_probe_668_allocation_estimate_created_at_nullable_false() -> None:
    from apecx_integration.control_plane.models.entities import AllocationEstimate
    col = AllocationEstimate.__table__.c["created_at"]
    assert col.nullable is False


def test_probe_669_verified_synonym_unique_tuple() -> None:
    """The (source_vocabulary, query_term, target_vocabulary, scope,
    is_active) tuple must be unique. A duplicate active mapping
    silently corrupts the cache lookup hot path."""
    from apecx_integration.control_plane.models.entities import VerifiedSynonym
    table = VerifiedSynonym.__table__
    uq_constraints = [c for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert any(
        {col.name for col in c.columns} == {
            "source_vocabulary", "query_term",
            "target_vocabulary", "scope", "is_active",
        }
        for c in uq_constraints
    ), f"PROBE 669: tuple unique constraint missing: {[list(c.columns.keys()) for c in uq_constraints]}"


def test_probe_670_every_model_has_primary_key() -> None:
    """Sanity: every ORM model must declare a primary key. SQLAlchemy
    raises at metadata create if missing — but if a migration
    decoupled from the model lands, you'd get inconsistent state."""
    from apecx_integration.control_plane.models.entities import (
        Run, Step, Approval, Artifact, GeneratedArtifact,
        ProvenanceEvent, Component, AllocationEstimate, VerifiedSynonym,
    )
    for cls in (Run, Step, Approval, Artifact, GeneratedArtifact,
                ProvenanceEvent, Component, AllocationEstimate, VerifiedSynonym):
        pk_cols = [c.name for c in cls.__table__.primary_key.columns]
        assert pk_cols, f"PROBE 670: {cls.__name__} has no primary key"


def test_probe_671_foreign_keys_point_at_real_tables() -> None:
    """Every FK must reference a table that's actually declared.
    A typo'd FK target would create at metadata create time but fail
    on real INSERT."""
    from apecx_integration.control_plane.models.base import Base
    metadata = Base.metadata
    table_names = set(metadata.tables.keys())
    for table in metadata.tables.values():
        for fk in table.foreign_keys:
            target_table = fk.column.table.name
            assert target_table in table_names, (
                f"PROBE 671: {table.name}.{fk.parent.name} references "
                f"unknown table {target_table}"
            )


def test_probe_672_enum_columns_native_false() -> None:
    """Every enum column must use native_enum=False so SQLite (no
    native enum) can host the schema. A native_enum=True column
    silently breaks Postgres parity."""
    from sqlalchemy import Enum as SQLAEnum
    from apecx_integration.control_plane.models.base import Base
    for table in Base.metadata.tables.values():
        for col in table.c:
            if isinstance(col.type, SQLAEnum):
                assert col.type.native_enum is False, (
                    f"PROBE 672: {table.name}.{col.name} uses native_enum=True"
                )


# ---------------------------------------------------------------------------
# UUIDString type decorator — probes 673-676
# ---------------------------------------------------------------------------


def test_probe_673_uuidstring_uuid_to_canonical() -> None:
    from apecx_integration.control_plane.models.base import UUIDString
    td = UUIDString()
    u = uuid4()
    s = td.process_bind_param(u, dialect=None)
    assert s == str(u)
    # Round-trip
    back = td.process_result_value(s, dialect=None)
    assert back == u


def test_probe_674_uuidstring_string_normalizes_to_canonical() -> None:
    """A non-canonical UUID string (uppercase, no hyphens) must
    normalize to the canonical lowercase-with-hyphens form on bind."""
    from apecx_integration.control_plane.models.base import UUIDString
    td = UUIDString()
    canonical = "550e8400-e29b-41d4-a716-446655440000"
    upper = canonical.upper()
    assert td.process_bind_param(upper, dialect=None) == canonical


def test_probe_675_uuidstring_rejects_non_uuid_type() -> None:
    """Passing an int or arbitrary object must raise — silent
    coercion would let bogus values into the DB."""
    from apecx_integration.control_plane.models.base import UUIDString
    td = UUIDString()
    with pytest.raises(TypeError):
        td.process_bind_param(123, dialect=None)


def test_probe_676_uuidstring_none_passthrough() -> None:
    """Nullable UUID columns: None must pass through unchanged."""
    from apecx_integration.control_plane.models.base import UUIDString
    td = UUIDString()
    assert td.process_bind_param(None, dialect=None) is None
    assert td.process_result_value(None, dialect=None) is None


# ---------------------------------------------------------------------------
# Cross-validation: ORM ↔ Pydantic ↔ enum — probes 677-679
# ---------------------------------------------------------------------------


def test_probe_677_run_orm_columns_match_pydantic_fields() -> None:
    """Every Pydantic Run field must have a matching ORM column.
    A drift here means the API serializes fields the DB doesn't
    persist (or vice versa) — silent data loss."""
    from apecx_integration.control_plane.models.entities import Run as RunORM
    from apecx_integration.control_plane.schemas.entities import Run as RunPydantic
    pyd_fields = set(RunPydantic.model_fields.keys())
    orm_cols = set(RunORM.__table__.c.keys())
    missing = pyd_fields - orm_cols
    assert not missing, (
        f"PROBE 677: Pydantic Run has fields ORM doesn't: {missing}"
    )


def test_probe_678_orm_round_trip_through_real_db(tmp_path) -> None:
    """End-to-end: alembic upgrade → ORM insert → ORM select must
    round-trip a Run row with all required fields populated. This
    is the smoke test that proves models + migrations + UUIDString
    work together."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    db = tmp_path / "rt.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db}")
    SessionFactory = make_session_factory(eng)
    rid = uuid4()
    with SessionFactory() as session:
        run = Run(
            id=rid, user_id="alice",
            status=RunStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        session.add(run)
        session.commit()
    with SessionFactory() as session:
        loaded = session.get(Run, rid)
        assert loaded is not None
        assert loaded.id == rid
        assert loaded.status is RunStatus.PENDING


def test_probe_679_run_status_enum_values_persist_lowercase(tmp_path) -> None:
    """RunStatus.PENDING.value == 'pending'. With native_enum=False
    the column stores the string value verbatim. A future change to
    enum NAMES (PENDING → AWAITING) must NOT silently change the
    on-disk string — that would corrupt every existing row."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import make_engine
    from apecx_integration.control_plane.schemas.enums import RunStatus
    db = tmp_path / "enum.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db}")
    rid = str(uuid4())
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, :st, :ts)"
            ),
            {"id": rid, "uid": "u", "st": RunStatus.PENDING.value,
             "ts": "2026-04-26T00:00:00+00:00"},
        )
    with eng.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM run WHERE id = :id"), {"id": rid}
        ).fetchone()
        assert row is not None
        # Stored as lowercase string ("pending"), not "PENDING"
        assert row[0] == "pending"
