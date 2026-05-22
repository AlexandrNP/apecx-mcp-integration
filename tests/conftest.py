"""Pytest configuration for apecx-integration.

Markers are declared in pyproject.toml. This file exists to be imported by pytest
and to give per-test-suite fixtures a stable home as the suite grows.
"""

from __future__ import annotations


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
