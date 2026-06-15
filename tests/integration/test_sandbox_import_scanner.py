"""T13 Phase 1 — import-whitelist scanner tests.

Covers the Phase 1 scanner:
  - AC1: scanner identifies all import forms (``import x``, ``from x
    import y``, ``import x.y.z``, ``import x as y``, etc.).
  - AC2: whitelisted imports pass; non-whitelisted imports raise with
    a diagnostic that names the unwhitelisted package.
  - Dynamic-import / exec / eval are rejected regardless of whitelist.
  - Relative imports are rejected (novel artifacts have no package
    context).
  - Whitelist loader sanity (comments, blank lines, duplicates).
  - Syntax errors in source produce a clear diagnostic rather than
    crashing the caller.

These are unit-style tests — no LLM, no composer, no network. The
matching integration test (scanner invoked by the composer on a real
generated artifact) lands with the composer itself (Phase 2 task).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.sandbox import (
    ImportScanner,
    ScanViolation,
    load_whitelist,
    scan_python_source,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WHITELIST_PATH = REPO_ROOT / "configs" / "sandbox" / "import_whitelist.txt"


# ---------------------------------------------------------------------------
# AC1 — scanner identifies import forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected_modules",
    [
        ("import pandas", ["pandas"]),
        ("import pandas as pd", ["pandas"]),
        ("import pandas.io", ["pandas"]),
        ("import pandas.io.json as pj", ["pandas"]),
        ("from pandas import DataFrame", ["pandas"]),
        ("from pandas.io import json", ["pandas"]),
        ("from pandas import DataFrame as DF", ["pandas"]),
        ("import numpy; import pandas", ["numpy", "pandas"]),
        ("import numpy\nimport pandas", ["numpy", "pandas"]),
        (
            "from typing import Any, Callable\nfrom collections import OrderedDict",
            ["typing", "typing", "collections"],
        ),
    ],
)
def test_scanner_identifies_import_forms(source: str, expected_modules: list[str]):
    """AC1: every import form recognised; ``module`` is the top-level
    package so whitelist checks are stable across re-exports.
    """
    result = scan_python_source(source)
    assert [imp.module for imp in result.imports] == expected_modules


def test_scanner_records_lineno():
    """Diagnostics need line numbers so reviewers can jump to the
    offending import without scanning the whole file.
    """
    source = "\n\nimport pandas\n\nfrom numpy import array\n"
    result = scan_python_source(source)
    assert [(i.module, i.lineno) for i in result.imports] == [
        ("pandas", 3),
        ("numpy", 5),
    ]


# ---------------------------------------------------------------------------
# AC2 — whitelist pass / fail
# ---------------------------------------------------------------------------


def test_whitelisted_imports_pass():
    result = scan_python_source(
        "import pandas\nfrom numpy import array\n",
        whitelist=frozenset({"pandas", "numpy"}),
    )
    assert result.ok
    assert result.violations == []


def test_non_whitelisted_imports_violate():
    result = scan_python_source(
        "import subprocess\nfrom socket import socket\n",
        whitelist=frozenset({"pandas", "numpy"}),
    )
    assert not result.ok
    assert any("'subprocess'" in v for v in result.violations)
    assert any("'socket.socket'" in v or "'socket'" in v for v in result.violations)


def test_whitelist_none_accepts_any_import():
    """When whitelist is None, the scanner only enforces the
    banned-constructs rules — import policy is the caller's job."""
    result = scan_python_source("import subprocess\nimport socket")
    assert result.ok
    assert [i.module for i in result.imports] == ["subprocess", "socket"]


# ---------------------------------------------------------------------------
# Dynamic-import / exec / eval — rejected regardless of whitelist
# ---------------------------------------------------------------------------


def test_eval_banned_even_when_builtins_whitelisted():
    """``eval`` is in ``__builtins__`` so the whitelist can't gate it;
    it has to be rejected at the construct level."""
    result = scan_python_source(
        "x = eval('1+1')",
        whitelist=frozenset({"pandas"}),
    )
    assert not result.ok
    assert any("eval" in v for v in result.violations)


def test_exec_banned():
    result = scan_python_source("exec('import os')", whitelist=frozenset({"os"}))
    assert not result.ok
    assert any("exec" in v for v in result.violations)


def test_dunder_import_banned():
    result = scan_python_source(
        "mod = __import__('os')",
        whitelist=frozenset({"os"}),
    )
    assert not result.ok
    assert any("__import__" in v for v in result.violations)


def test_importlib_import_module_banned():
    """``importlib`` itself may be on the whitelist (type-checking use),
    but ``importlib.import_module()`` CALLS are banned.
    """
    result = scan_python_source(
        "import importlib\nmod = importlib.import_module('os')\n",
        whitelist=frozenset({"importlib", "os"}),
    )
    assert not result.ok
    assert any("importlib.import_module" in v for v in result.violations)


def test_compile_banned():
    result = scan_python_source(
        "code = compile('print(1)', '<x>', 'exec')",
        whitelist=frozenset(),
    )
    assert not result.ok
    assert any("compile" in v for v in result.violations)


# ---------------------------------------------------------------------------
# Relative-import rejection
# ---------------------------------------------------------------------------


def test_relative_import_rejected():
    """Novel single-file artifacts have no package context. A ``from .
    import x`` either is a bug in the generator or an attempted escape.
    """
    result = scan_python_source("from . import helpers", whitelist=frozenset())
    assert not result.ok
    assert any("relative imports" in v for v in result.violations)


# ---------------------------------------------------------------------------
# ScanViolation exception path
# ---------------------------------------------------------------------------


def test_scan_violation_message_lists_all_violations():
    """Callers that raise ScanViolation get a multi-line message naming
    every violation — otherwise a reviewer has to fix-run-fix-run instead
    of seeing everything at once.
    """
    result = scan_python_source(
        "import subprocess\nimport socket\neval('1')\n",
        whitelist=frozenset(),
    )
    exc = ScanViolation(result)
    message = str(exc)
    assert "subprocess" in message
    assert "socket" in message
    assert "eval" in message


# ---------------------------------------------------------------------------
# Syntax-error handling
# ---------------------------------------------------------------------------


def test_syntax_error_recorded_as_violation():
    """A syntax error is a violation, not an exception the scanner
    re-raises — callers should treat "can't parse" the same as "failed
    import check."
    """
    result = scan_python_source("def broken(:\n    pass")
    assert not result.ok
    assert any("syntax error" in v for v in result.violations)


# ---------------------------------------------------------------------------
# Whitelist loader
# ---------------------------------------------------------------------------


def test_default_whitelist_loads_cleanly():
    """The shipped whitelist file parses without error and includes the
    core expected entries."""
    wl = load_whitelist(DEFAULT_WHITELIST_PATH)
    assert "pandas" in wl
    assert "numpy" in wl
    assert "nanobrain" in wl
    # Intentionally NOT on the whitelist:
    assert "subprocess" not in wl
    assert "socket" not in wl


def test_whitelist_loader_ignores_comments_and_blanks(tmp_path: Path):
    p = tmp_path / "wl.txt"
    p.write_text("# comment line\n\npandas\n  # indented comment \nnumpy\n\n")
    wl = load_whitelist(p)
    assert wl == frozenset({"pandas", "numpy"})


def test_whitelist_loader_rejects_dotted_entries(tmp_path: Path):
    """Dotted entries like ``pandas.io`` are user confusion — the scanner
    matches on top-level. Catch the typo at load time.
    """
    p = tmp_path / "wl.txt"
    p.write_text("pandas.io\n")
    with pytest.raises(ValueError, match="top-level package names"):
        load_whitelist(p)


# ---------------------------------------------------------------------------
# End-to-end: the shipped whitelist against a plausible novel artifact
# ---------------------------------------------------------------------------


def test_end_to_end_realistic_novel_artifact():
    """A fake "composer-generated" module that imports only whitelisted
    things should pass against the shipped whitelist.
    """
    source = """
from dataclasses import dataclass
from typing import Any
import pandas as pd
import numpy as np
from apecx_integration.composition.steps.db_integration_wrappers import EntityExtractionStep

@dataclass
class Result:
    rows: list[dict[str, Any]]

def join_vaccine_ids(df: pd.DataFrame, ids: list[str]) -> pd.DataFrame:
    mask = df['Vaccine_ID'].isin(ids)
    return df.loc[mask]
"""
    wl = load_whitelist(DEFAULT_WHITELIST_PATH)
    result = ImportScanner(whitelist=wl).scan(source)
    assert result.ok, f"expected pass; got violations: {result.violations}"
