"""CGU-P1-T1 follow-up — pin the sandbox's stdin-based script delivery.

Why this test file exists: 2026-05-12, a SciCode validation sweep at
n=20 crashed with ``OSError: [Errno 7] Argument list too long`` inside
``subprocess.run`` because the sandbox passed the assembled script via
``python -c "<script>"``. Some SciCode problems inline base64-encoded
pickled ``target`` arrays in ``setup_code``; once those exceed macOS's
~256 KB ARG_MAX the whole sweep crashes mid-run rather than skipping
the offending problem.

The fix routes scripts through stdin (``python -`` + ``input=script``),
which has no length limit. These tests pin that fix so a future
"simplification" that reverts to ``-c`` blows up here, not in a 10-
minute sweep.
"""

from __future__ import annotations

from tests.benchmarks.sandbox import run_in_subprocess


def test_minimal_pass_path():
    """Smoke: a trivial setup + candidate + assert all succeeds."""
    result = run_in_subprocess(
        candidate_code="def add(a, b): return a + b",
        setup_code="",
        test_code="assert add(2, 3) == 5",
        timeout_seconds=10.0,
    )
    assert result.passed is True
    assert result.timed_out is False
    assert result.exit_code == 0


def test_minimal_fail_path():
    """Smoke: an asserted contradiction surfaces as exit_code != 0."""
    result = run_in_subprocess(
        candidate_code="def add(a, b): return a + b",
        setup_code="",
        test_code="assert add(2, 3) == 99",
        timeout_seconds=10.0,
    )
    assert result.passed is False
    assert result.exit_code != 0
    assert "AssertionError" in result.stderr


def test_large_script_does_not_exceed_arg_max():
    """Regression: a 1 MB script must run cleanly through the sandbox.

    macOS ARG_MAX is ~256 KB; Linux is typically ~128 KB. A 1 MB script
    is well past either limit, so this test fails fast (with
    ``OSError: [Errno 7] Argument list too long``) if anyone reverts
    the stdin fix. The script content is a no-op string literal so
    runtime stays low and the only failure mode tested is the
    delivery mechanism, not execution.
    """
    # 1_000_000 byte string literal. Encoded in the script as ``s = '<...>'``,
    # the total `setup_code` size is just over 1 MB. With ARG_MAX
    # ~256 KB, the legacy ``-c`` path would raise OSError here.
    huge_literal = "a" * 1_000_000
    setup = f"s = {huge_literal!r}\n"
    candidate = "def length():\n    return len(s)\n"
    test = "assert length() == 1_000_000\n"

    result = run_in_subprocess(
        candidate_code=candidate,
        setup_code=setup,
        test_code=test,
        timeout_seconds=15.0,
    )
    assert result.passed is True, (
        f"large-script sandbox call failed: stderr={result.stderr[:300]!r}"
    )


def test_extra_env_propagates_to_subprocess():
    """``extra_env`` should reach the subprocess so dataset-specific
    env vars (e.g. ``APECX_LLM_BASE_URL`` for workflow tests) override
    correctly."""
    result = run_in_subprocess(
        candidate_code="",
        setup_code="import os",
        test_code="assert os.environ['CGU_T1_SENTINEL'] == 'present'",
        timeout_seconds=5.0,
        extra_env={"CGU_T1_SENTINEL": "present"},
    )
    assert result.passed is True


def test_timeout_surfaces_as_dedicated_flag():
    """Sandbox must distinguish ``timed_out=True`` from a normal
    nonzero exit so the scorer can bucket timeouts separately."""
    result = run_in_subprocess(
        candidate_code="",
        setup_code="import time",
        test_code="time.sleep(10)",
        timeout_seconds=1.0,
    )
    assert result.passed is False
    assert result.timed_out is True
    assert result.exit_code is None
