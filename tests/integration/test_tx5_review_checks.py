"""TX5 — tests for the agent-output review-harness mechanical checks.

Each check is a standalone Python script under ``scripts/checks/`` that
returns exit code 0 on pass and 1 on failure. The tests exercise each
against good+bad fixtures under
``tests/integration/fixtures/tx5/`` and assert the exit code shape.

This is the "mock half" — the corresponding integration would be a CI
workflow that runs the same scripts on every PR. CI wiring is gated on
TX2 AC2 (repo going onto GitHub); the scripts themselves work standalone
now and can be invoked from pre-commit, make targets, or a shell.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_IMPORTS = REPO_ROOT / "scripts" / "checks" / "imports_resolve.py"
SCRIPT_STEP_AUTHORING = REPO_ROOT / "scripts" / "checks" / "step_authoring.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "integration" / "fixtures" / "tx5"


def _run(script: Path, target: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the check script with a target directory. Returns the
    full CompletedProcess so tests can assert exit code AND stderr.
    """
    return subprocess.run(
        [sys.executable, str(script), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC2 — imports_resolve
# ---------------------------------------------------------------------------

def test_imports_resolve_passes_on_good_fixture(tmp_path: Path):
    """A directory containing only stdlib/local imports should pass."""
    shutil.copy(FIXTURE_DIR / "good_import.py", tmp_path)
    result = _run(SCRIPT_IMPORTS, tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"


def test_imports_resolve_rejects_hallucinated_import(tmp_path: Path):
    """Nonexistent package → exit 1 + stderr names the offending module."""
    shutil.copy(FIXTURE_DIR / "bad_import.py", tmp_path)
    result = _run(SCRIPT_IMPORTS, tmp_path)
    assert result.returncode == 1
    assert "definitely_not_a_real_package" in result.stderr
    assert "also_fake_pkg" in result.stderr


def test_imports_resolve_passes_on_real_src_tree():
    """Smoke: the actual ``src/`` should already be clean (if it's not,
    this test is an early warning that ruff/pytest aren't the only things
    that can go red).
    """
    result = _run(SCRIPT_IMPORTS, REPO_ROOT / "src")
    assert result.returncode == 0, (
        f"src/ tree has unresolvable imports:\n{result.stderr}"
    )


def test_imports_resolve_skips_stdlib():
    """``sys.stdlib_module_names`` gives us stdlib short-circuit. If
    stdlib entries start failing find_spec, we want to know — but for
    this test just verify the happy path passes without flagging them.
    """
    result = _run(SCRIPT_IMPORTS, FIXTURE_DIR)
    # The fixture dir has bad_import.py too, so the exit code will
    # reflect those. Just assert stdlib names don't show up in stderr.
    for stdlib_name in ("json", "dataclasses", "typing", "pathlib"):
        assert f"{stdlib_name!r}" not in result.stderr, (
            f"stdlib {stdlib_name!r} should not appear in violations: {result.stderr}"
        )


def test_imports_resolve_rejects_missing_target():
    result = _run(SCRIPT_IMPORTS, Path("/nonexistent/dir/for/this/test"))
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# AC3 — step_authoring
# ---------------------------------------------------------------------------

def test_step_authoring_passes_on_good_fixture(tmp_path: Path):
    """A step with ``async def process`` and no ``execute`` override
    should pass."""
    target = tmp_path / "steps"
    target.mkdir()
    shutil.copy(FIXTURE_DIR / "good_step.py", target)
    result = _run(SCRIPT_STEP_AUTHORING, target)
    assert result.returncode == 0, f"stderr={result.stderr!r}"


def test_step_authoring_rejects_execute_override(tmp_path: Path):
    """Overriding execute is the primary failure mode per nanobrain's
    method-responsibility matrix."""
    target = tmp_path / "steps"
    target.mkdir()
    shutil.copy(FIXTURE_DIR / "bad_override_execute_step.py", target)
    result = _run(SCRIPT_STEP_AUTHORING, target)
    assert result.returncode == 1
    assert "execute" in result.stderr
    assert "BadStep" in result.stderr


def test_step_authoring_rejects_missing_process(tmp_path: Path):
    target = tmp_path / "steps"
    target.mkdir()
    shutil.copy(FIXTURE_DIR / "bad_missing_process_step.py", target)
    result = _run(SCRIPT_STEP_AUTHORING, target)
    assert result.returncode == 1
    assert "process" in result.stderr
    assert "IncompleteStep" in result.stderr


def test_step_authoring_only_scans_step_named_files(tmp_path: Path):
    """A non-step file (no ``step`` in the name) is ignored, even if
    it contains a class that would fail the check. This keeps the scan
    tight — steps live in files with ``step`` in the name by convention.
    """
    target = tmp_path / "mixed"
    target.mkdir()
    # File NOT matching *step*.py — should be ignored.
    (target / "workflow_util.py").write_text(
        "class MyTool:\n    async def execute(self): pass\n"
    )
    result = _run(SCRIPT_STEP_AUTHORING, target)
    assert result.returncode == 0


def test_step_authoring_passes_on_real_src_tree():
    """Smoke: the current ``src/`` tree is compliant with the step rules."""
    result = _run(SCRIPT_STEP_AUTHORING, REPO_ROOT / "src")
    assert result.returncode == 0, (
        f"src/ tree has step-authoring violations:\n{result.stderr}"
    )
