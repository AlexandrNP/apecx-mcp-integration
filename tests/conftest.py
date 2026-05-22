"""Pytest configuration for apecx-integration.

Markers are declared in pyproject.toml. This file exists to be imported by pytest
and to give per-test-suite fixtures a stable home as the suite grows.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stop_infra_orchestrator_thread():
    """Stop any orchestrator background drive thread a test spawned.

    ``build_server()`` — and anything that boots the MCP server path —
    spawns the ``apecx-infra-orchestrator`` daemon thread, which runs
    ``start_all()`` + (in full mode) the workflow-tool pre-warm. Without
    teardown that thread outlives the test: it keeps logging into pytest's
    now-closed capture stream (``--- Logging error --- ValueError: I/O
    operation on closed file``) and can leak DB / conda-build work into
    unrelated tests.

    This stops + joins the thread after every test. It is a no-op when no
    thread was started (the common case) — a single cheap ``is_alive()``
    check. The import is lazy so the orchestrator module is not pulled into
    the import graph of tests that never touch it.
    """
    yield
    from apecx_integration.infrastructure.orchestrator import (
        stop_orchestrator_in_background_thread,
    )

    stop_orchestrator_in_background_thread(timeout=10.0)


def pytest_ignore_collect(collection_path) -> bool | None:
    """Exclude the codegen-benchmark problem TEMPLATES from normal collection.

    ``tests/benchmarks/problems/<name>/test_code.py`` are evaluation templates,
    not suite tests: each asserts a candidate symbol (e.g. ``DoubleStep``) at
    module scope and is meant to be PREPENDED with a generated candidate, then
    run in a subprocess by the bench sandbox (``tests/benchmarks/sandbox.py``).
    A plain ``pytest tests`` collection imports them standalone, so the assert
    fires at import time → collection ERROR. The bench runner executes them
    itself; they must never be collected by the normal suite.

    Implemented as a hook (not ``collect_ignore_glob``) because the latter's
    fnmatch patterns proved unreliable against the nested subtree here.
    """
    return "/benchmarks/problems/" in collection_path.as_posix()
