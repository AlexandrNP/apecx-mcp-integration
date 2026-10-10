"""Dependency-boundary + degrade-path guardian for the cycle-2 literature_qa ranker.

SYNTHETIC fixtures only — no real question, no real PMID, no network, no PubMed,
no ``rag`` extra. These tests are domain-blind: they prove MECHANISM, not science.

They pin four clean-install / degrade-loud properties after cycle-2 changed
``LiteratureRagStep._hybrid_rank`` (IDF-weighted keyword score + adaptive relevance
gate, ``import math``) and the ``facets.py`` matcher:

  1. No new module-scope optional-extra import in the three changed source files.
  2. The tool module and the reader-step module both IMPORT with
     ``sentence_transformers`` / ``faiss`` made un-importable.
  3. When the embedding model cannot load, ``_hybrid_rank`` degrades LOUD to
     keyword-only, STILL gates + caps (never the full unranked set), never raises.
  4. The existing isolation test ``test_module_imports_without_rag_extra`` passes
     in a fresh interpreter (the combined-run failure is pre-existing sys.modules
     pollution from a sibling faiss smoke test, not a cycle-2 regression).

Real-data parity partner: tests/integration/test_literature_rag_tool_live.py and
tests/unit/test_literature_qa_tool_smoke.py's gated-integration case.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"

# The three files cycle-2 touched (reader-step ranker, shared facets, MCP tool).
_CHANGED_FILES = [
    _SRC / "apecx_integration/composition/workflows/literature_rag/steps/literature_rag_step.py",
    _SRC / "apecx_integration/agents/literature/facets.py",
    _SRC / "apecx_integration/mcp_surface/tools/literature_qa.py",
]

_EXTRA_IMPORT_RE = (
    r"^(import|from) "
    r"(sentence_transformers|faiss|globus_sdk|globus_compute_sdk|keyring|academy)\b"
)


def _subprocess_env() -> dict:
    """Env that forces this worktree's src onto the path (the editable install is
    pinned to a DIFFERENT worktree — mirrors scripts/run_tests.sh PYTHONPATH=src)."""
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    return env


# --------------------------------------------------------------------------- #
# Case 1 — no new module-scope optional-extra import                          #
# --------------------------------------------------------------------------- #
def test_changed_files_have_no_module_scope_extra_import():
    """The CLAUDE.md detection grep must find ZERO module-scope extra imports in
    any of the three cycle-2 files (the embedding import stays inside a method)."""
    import re

    pattern = re.compile(_EXTRA_IMPORT_RE)
    offenders: list[str] = []
    for path in _CHANGED_FILES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.match(line):
                offenders.append(f"{path.name}:{lineno}: {line}")
    assert offenders == [], f"module-scope extra import(s) added by cycle-2: {offenders}"


def test_import_math_is_stdlib_not_an_extra():
    """``import math`` (added in cycle-2) is the stdlib module — not an optional extra."""
    reader = _CHANGED_FILES[0].read_text(encoding="utf-8")
    assert "\nimport math\n" in reader  # present at module scope
    assert math_is_stdlib()


def math_is_stdlib() -> bool:
    import math

    # stdlib math is a builtin/extension with no site-packages file path.
    return getattr(math, "__file__", None) is None or "site-packages" not in (
        getattr(math, "__file__", "") or ""
    )


# --------------------------------------------------------------------------- #
# Case 2 — import without the rag extra                                        #
# --------------------------------------------------------------------------- #
def test_tool_and_reader_import_without_rag_extra():
    """With sentence_transformers + faiss made un-importable, importing the tool
    module AND the reader-step module must both SUCCEED (lazy extra imports)."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SRC)!r})\n"
        "BLOCKED = {'sentence_transformers', 'faiss'}\n"
        "class _Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ModuleNotFoundError('blocked extra: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "for mod in BLOCKED:\n"
        "    try:\n"
        "        __import__(mod)\n"
        "        raise SystemExit('FAIL: ' + mod + ' still importable')\n"
        "    except ModuleNotFoundError:\n"
        "        pass\n"
        "import apecx_integration.mcp_surface.tools.literature_qa  # noqa: F401\n"
        "import apecx_integration.composition.workflows.literature_rag.steps."
        "literature_rag_step  # noqa: F401\n"
        "assert 'sentence_transformers' not in sys.modules\n"
        "assert 'faiss' not in sys.modules\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env=_subprocess_env(),
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"import failed without rag extra:\nSTDOUT{proc.stdout}\nSTDERR{proc.stderr}"
    )
    assert proc.stdout.strip().endswith("OK")


# --------------------------------------------------------------------------- #
# Case 3 — degrade LOUD to keyword-only, still gated + capped, never raises    #
# --------------------------------------------------------------------------- #
class _CaptureLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):  # pragma: no cover - not asserted
        pass


def _rec(pmid: str, title: str = "", abstract: str = "") -> dict:
    return {"pmid": pmid, "title": title, "abstract": abstract}


def _reader_step_embed_broken():
    """A real reader step whose embedding model load RAISES (simulates no rag extra)."""
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    steps_dir = Path(sys.modules[LiteratureRagStep.__module__].__file__).resolve().parent
    step = LiteratureRagStep.from_config(str(steps_dir / "literature_rag_step.yml"))
    step.nb_logger = _CaptureLogger()
    # The embedding boundary: loading the model raises -> _embed_sims raises ->
    # _hybrid_rank catches, warns, degrades to keyword-only.
    step._get_embed_model = lambda: (_ for _ in ()).throw(RuntimeError("no rag extra"))
    return step


def test_degrade_loud_keyword_only_respects_floor():
    """Sharp IDF drop (only 2 of 18+ records relevant) + embedding unavailable:
    the gate would keep 2, but the floor of 3 lifts it to exactly 3 — fewer than
    15, fewer than the full set, keyword signal still ranks relevant records first,
    a WARNING is logged, and nothing raises."""
    step = _reader_step_embed_broken()
    question = "alpha beta gamma delta epsilon"
    records = [_rec(f"noise{i}", title="unrelated filler words here") for i in range(18)]
    records.insert(0, _rec("hitA", title="alpha beta gamma delta epsilon"))
    records.insert(0, _rec("hitB", title="alpha beta gamma delta epsilon"))

    ranked = step._hybrid_rank(question, records, 15)

    assert len(ranked) == 3  # gate would give 2; recall floor lifts to 3
    assert len(ranked) < 15  # never a padded cap
    assert len(ranked) < len(records)  # never the full unranked set
    assert {r["pmid"] for r in ranked[:2]} == {"hitA", "hitB"}  # keyword signal still leads
    assert any("keyword" in w.lower() for w in step.nb_logger.warnings)  # degrade-LOUD


def test_degrade_loud_keyword_only_gate_cuts_tail_above_floor():
    """When more than the floor are strongly relevant, the gate keeps exactly that
    head (8) and drops the zero-score tail (12) — proving the gate, not just the
    floor, is doing the cut: fewer than 15, never the full 20."""
    step = _reader_step_embed_broken()
    question = "alpha beta gamma delta epsilon"
    relevant = [_rec(f"hit{i}", title="alpha beta gamma delta epsilon") for i in range(8)]
    tail = [_rec(f"noise{i}", title="totally unrelated filler") for i in range(12)]
    records = relevant + tail

    ranked = step._hybrid_rank(question, records, 15)

    assert len(ranked) == 8  # the gate kept the relevant head, dropped the 12 zero tail
    assert len(ranked) < 15
    assert len(ranked) < len(records)
    assert {r["pmid"] for r in ranked} == {f"hit{i}" for i in range(8)}


def test_degrade_loud_never_raises_and_never_returns_full_set():
    """Over a larger synthetic set with a graded relevance drop, the keyword-only
    degrade path returns a gated+capped subset and never raises."""
    step = _reader_step_embed_broken()
    question = "alpha beta gamma delta epsilon zeta"
    records = []
    records += [_rec(f"top{i}", title="alpha beta gamma delta epsilon zeta") for i in range(5)]
    records += [_rec(f"one{i}", title="alpha") for i in range(10)]  # weak single-facet tail
    records += [_rec(f"zero{i}", title="nothing relevant here") for i in range(10)]

    ranked = step._hybrid_rank(question, records, 15)

    assert 3 <= len(ranked) <= 15
    assert len(ranked) < len(records)
    assert step.nb_logger.warnings  # loud


# --------------------------------------------------------------------------- #
# Case 4 — isolation triage: the existing import-cleanliness test passes alone #
# --------------------------------------------------------------------------- #
def test_tool_import_leaves_sys_modules_clean_in_fresh_interpreter():
    """Replicates test_module_imports_without_rag_extra's property in a FRESH
    interpreter (no sibling-test sys.modules pollution): importing the tool must
    NOT drag in faiss / sentence_transformers. This is the isolation triage —
    the combined-run failure is pre-existing pollution, not a cycle-2 regression."""
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SRC)!r})\n"
        "import apecx_integration.mcp_surface.tools.literature_qa  # noqa: F401\n"
        "assert 'faiss' not in sys.modules, 'faiss leaked at tool import'\n"
        "assert 'sentence_transformers' not in sys.modules, 'ST leaked at tool import'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env=_subprocess_env(),
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"STDOUT{proc.stdout}\nSTDERR{proc.stderr}"
    assert proc.stdout.strip().endswith("OK")
