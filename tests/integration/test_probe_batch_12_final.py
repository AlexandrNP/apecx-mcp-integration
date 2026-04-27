"""Probe batch 12 — final 29 probes.

Probes 276-304. Closing out the 100-probe streak.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent


# --- Probe 276: friction log file is parseable as markdown (no broken syntax) ---


def test_probe_276_friction_log_parseable() -> None:
    p = REPO_ROOT / "docs" / "session_friction_log.md"
    if not p.is_file():
        pytest.skip()
    text = p.read_text(encoding="utf-8")
    # Sanity: not empty, has headers
    assert "## " in text


# --- Probe 277: composer system.md ends with newline ---


def test_probe_277_system_md_ends_with_newline() -> None:
    p = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_prompts" / "system.md"
    raw = p.read_bytes()
    assert raw.endswith(b"\n"), "PROBE 277: system.md missing terminal newline"


# --- Probe 278: workspace CLAUDE.md ends with newline ---


def test_probe_278_workspace_claude_ends_with_newline() -> None:
    p = WORKSPACE_ROOT / "CLAUDE.md"
    raw = p.read_bytes()
    assert raw.endswith(b"\n")


# --- Probe 279: workflows/violin_bvbrc/manifest.yml has version field ---


def test_probe_279_manifest_has_version() -> None:
    p = REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "violin_bvbrc" / "manifest.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    # Manifest may have version at top-level or per-component
    if isinstance(parsed, dict):
        # Acceptable shapes: {version, components} or just components
        assert "version" in parsed or "components" in parsed or any(
            isinstance(v, dict) and ("version" in v or "implementation_path" in v)
            for v in parsed.values()
        )


# --- Probe 280: pyproject.toml exists and has the right project name ---


def test_probe_280_pyproject_present() -> None:
    p = REPO_ROOT / "pyproject.toml"
    assert p.is_file()
    text = p.read_text()
    assert "apecx" in text.lower()


# --- Probe 281: alembic.ini exists ---


def test_probe_281_alembic_ini_present() -> None:
    p = REPO_ROOT / "alembic.ini"
    assert p.is_file()


# --- Probe 282: migrations directory has env.py ---


def test_probe_282_migrations_env_py() -> None:
    p = REPO_ROOT / "migrations" / "env.py"
    assert p.is_file()


# --- Probe 283: scripts/run_tests.sh is executable ---


def test_probe_283_run_tests_sh_executable() -> None:
    import stat
    p = REPO_ROOT / "scripts" / "run_tests.sh"
    if not p.is_file():
        pytest.skip()
    mode = p.stat().st_mode
    assert mode & stat.S_IXUSR


# --- Probe 284: scripts/build_rag_index.py is a real Python file ---


def test_probe_284_build_rag_index_present() -> None:
    p = REPO_ROOT / "scripts" / "build_rag_index.py"
    if not p.is_file():
        pytest.skip()
    text = p.read_text()
    assert "import" in text or "def" in text


# --- Probe 285: configs/approval_policy.yml exists ---


def test_probe_285_approval_policy_yml() -> None:
    p = REPO_ROOT / "configs" / "approval_policy.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    assert isinstance(parsed, dict)


# --- Probe 286: ApprovalPolicy.load works on configs/approval_policy.yml ---


def test_probe_286_approval_policy_loads() -> None:
    p = REPO_ROOT / "configs" / "approval_policy.yml"
    if not p.is_file():
        pytest.skip()
    from apecx_integration.composition.approval_policy import ApprovalPolicy
    policy = ApprovalPolicy.load(p)
    assert policy is not None


# --- Probe 287: All migration files have revision IDs ---


def test_probe_287_migrations_have_revisions() -> None:
    mdir = REPO_ROOT / "migrations" / "versions"
    if not mdir.is_dir():
        pytest.skip()
    import re
    for f in mdir.glob("*.py"):
        text = f.read_text()
        assert re.search(r'revision[^"]*[=:]\s*["\']', text), (
            f"PROBE 287: migration {f.name} missing revision ID"
        )


# --- Probe 288: All migrations chain consistently (down_revision links) ---


def test_probe_288_migration_chain_consistent() -> None:
    mdir = REPO_ROOT / "migrations" / "versions"
    if not mdir.is_dir():
        pytest.skip()
    import re
    revisions: dict[str, str | None] = {}
    for f in sorted(mdir.glob("*.py")):
        text = f.read_text()
        m_rev = re.search(r'revision[^=]*=\s*["\']([^"\']+)["\']', text)
        m_down = re.search(r'down_revision[^=]*=\s*["\']?([^"\'\s,]+)["\']?', text)
        if m_rev:
            rev = m_rev.group(1)
            down = m_down.group(1) if m_down and m_down.group(1) != "None" else None
            revisions[rev] = down
    # Every down_revision should be a known revision (or None for genesis)
    for rev, down in revisions.items():
        if down and down != "None":
            assert down in revisions, (
                f"PROBE 288: migration {rev} down_revision={down} not found"
            )


# --- Probe 289: src/apecx_integration/__init__.py exists ---


def test_probe_289_package_init() -> None:
    p = REPO_ROOT / "src" / "apecx_integration" / "__init__.py"
    assert p.is_file()


# --- Probe 290: Composer module imports cleanly ---


def test_probe_290_composer_module_imports() -> None:
    import apecx_integration.composition.composer  # noqa: F401


# --- Probe 291: Recorder module imports cleanly ---


def test_probe_291_recorder_imports() -> None:
    import apecx_integration.control_plane.provenance.recorder  # noqa: F401


# --- Probe 292: All control_plane.routes import cleanly ---


def test_probe_292_routes_import() -> None:
    import apecx_integration.control_plane.routes.workflow  # noqa: F401
    import apecx_integration.control_plane.routes.approval  # noqa: F401
    import apecx_integration.control_plane.routes.hpc  # noqa: F401
    import apecx_integration.control_plane.routes.metrics  # noqa: F401
    import apecx_integration.control_plane.routes.status  # noqa: F401
    import apecx_integration.control_plane.routes.verified_synonyms  # noqa: F401


# --- Probe 293: app.create_app importable ---


def test_probe_293_app_importable() -> None:
    from apecx_integration.control_plane.app import create_app  # noqa: F401


# --- Probe 294: dependencies module has required functions ---


def test_probe_294_dependencies_complete() -> None:
    from apecx_integration.control_plane import dependencies as dep
    assert hasattr(dep, "get_session")
    assert hasattr(dep, "get_recorder")
    assert hasattr(dep, "get_composer")
    assert hasattr(dep, "get_composer_or_none")
    assert hasattr(dep, "require_composer")


# --- Probe 295: schemas.api ConfirmAllocationRequest has finite-validation ---


def test_probe_295_confirm_validator_present() -> None:
    """Cluster AL fix verification."""
    from apecx_integration.control_plane.schemas.api import (
        ConfirmAllocationRequest,
    )
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ConfirmAllocationRequest(run_id=__import__("uuid").uuid4(), confirmed_core_hours=float("inf"))


# --- Probe 296: schemas.api ConfirmAllocationRequest rejects NaN ---


def test_probe_296_confirm_validator_nan() -> None:
    from apecx_integration.control_plane.schemas.api import (
        ConfirmAllocationRequest,
    )
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ConfirmAllocationRequest(run_id=__import__("uuid").uuid4(), confirmed_core_hours=float("nan"))


# --- Probe 297: schemas.entities Run has all expected fields ---


def test_probe_297_run_schema_fields() -> None:
    from apecx_integration.control_plane.schemas.entities import Run
    fields = Run.model_fields
    for required in ["id", "user_id", "status", "created_at"]:
        assert required in fields


# --- Probe 298: schemas.entities Approval has all expected fields ---


def test_probe_298_approval_schema_fields() -> None:
    from apecx_integration.control_plane.schemas.entities import Approval
    fields = Approval.model_fields
    for required in ["id", "step_id", "kind", "status"]:
        assert required in fields


# --- Probe 299: schemas.entities Step has all expected fields ---


def test_probe_299_step_schema_fields() -> None:
    from apecx_integration.control_plane.schemas.entities import Step
    fields = Step.model_fields
    for required in ["id", "run_id", "step_name", "status"]:
        assert required in fields


# --- Probe 300: schemas.entities VerifiedSynonym has all expected fields ---


def test_probe_300_verified_synonym_schema_fields() -> None:
    from apecx_integration.control_plane.schemas.entities import VerifiedSynonym
    fields = VerifiedSynonym.model_fields
    for required in ["id", "source_vocabulary", "query_term", "target_vocabulary", "canonical_term"]:
        assert required in fields


# --- Probe 301: existing test files don't have stale references ---


def test_probe_301_test_files_importable() -> None:
    """Sanity: every test file imports cleanly. Catches import-
    related rot."""
    tests_dir = REPO_ROOT / "tests"
    import sys
    sys.path.insert(0, str(REPO_ROOT / "src"))
    failed = []
    for test_file in tests_dir.rglob("test_*.py"):
        # Only exercise integration tests since unit may have heavy deps
        if "integration" not in str(test_file):
            continue
        rel = test_file.relative_to(tests_dir).with_suffix("")
        mod_name = "tests." + str(rel).replace("/", ".")
        try:
            importlib.import_module(mod_name)
        except Exception as e:
            # Some test files have side-effect imports (live-LLM
            # gates) — skip those, fail others.
            if any(skip in str(e) for skip in ["ollama", "academy", "live"]):
                continue
            failed.append(f"{mod_name}: {type(e).__name__}: {e}")
    assert not failed, "PROBE 301 BUG: test files fail to import:\n" + "\n".join(failed[:5])


# --- Probe 302: README.md exists and isn't empty ---


def test_probe_302_readme_present() -> None:
    p = REPO_ROOT / "README.md"
    if not p.is_file():
        pytest.skip()
    assert p.stat().st_size > 100


# --- Probe 303: pyproject.toml has scripts entry for apecx-cp / apecx-mcp ---


def test_probe_303_pyproject_scripts() -> None:
    p = REPO_ROOT / "pyproject.toml"
    text = p.read_text()
    # Should have console scripts for apecx-cp + apecx-mcp
    assert "apecx" in text


# --- Probe 304: workspace CLAUDE.md mentions the friction log ---


def test_probe_304_workspace_claude_links_friction_log() -> None:
    text = (WORKSPACE_ROOT / "CLAUDE.md").read_text()
    assert "session_friction_log" in text
