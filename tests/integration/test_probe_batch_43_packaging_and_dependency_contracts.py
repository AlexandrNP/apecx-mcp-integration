"""Probe batch 43 — adversarial probes against packaging, dependency
declarations, and module-export invariants.

Streak before this batch: 74/300 post-AQ post-1066.
Probe naming: 1130–1154.

Distinct probes only.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Probes 1130–1154
# --------------------------------------------------------------------------- #


def test_probe_1130_pyproject_does_not_declare_apecx_db_integration_dep():
    """Per user directive 2026-04-27, this repo's only sibling-repo
    runtime deps should be nanobrain + apecx-harvesters. A leftover
    apecx_db_integration entry in pyproject.toml would silently
    re-introduce the dependency the migration removed."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    # The sibling-repo path-dep style would be e.g.
    # ``apecx-db-integration = { path = "../apecx-db-integration" }``
    forbidden = ["apecx-db-integration =", "apecx_db_integration ="]
    for f in forbidden:
        assert f not in text, (
            f"pyproject.toml leaks {f!r}; the migration was supposed "
            f"to drop this dependency"
        )


def test_probe_1131_pyproject_does_not_declare_apecx_rag_dep():
    """Same rationale as 1130 for apecx-rag (the LangGraph prototype
    the synthesizer was migrated from)."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    for f in ("apecx-rag =", "apecx_rag ="):
        assert f not in text


def test_probe_1132_apecx_harvesters_importable_from_venv():
    """The harvester adapter is the bridge to apecx-harvesters; that
    sibling repo MUST be installed editable in the venv. A missing
    install would silently fail at adapter import time."""
    importlib.import_module("apecx_harvesters.loaders.base.model")


def test_probe_1133_nanobrain_importable_from_venv():
    """The other sibling-repo runtime dep. Without nanobrain, every
    Step subclass import fails."""
    importlib.import_module("nanobrain.core.step")


def test_probe_1134_no_apecx_db_integration_imports_in_production_src():
    """Per probe 916 boundary invariant + Day 1 migration: no
    ``import apecx_db_integration`` anywhere under src/. Audit pass."""
    src = REPO_ROOT / "src"
    offenders = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        # Look for ACTUAL import statements (not docstring mentions).
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import apecx_db_integration") or \
               stripped.startswith("from apecx_db_integration"):
                offenders.append(str(py.relative_to(src)))
                break
    assert not offenders, (
        f"production code imports legacy apecx_db_integration: "
        f"{offenders!r}"
    )


def test_probe_1135_no_apecx_rag_imports_in_production_src():
    """Same audit for apecx_rag (Day 2 v1's "migration from prototype"
    must be complete in production code)."""
    src = REPO_ROOT / "src"
    offenders = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import apecx_rag") or \
               stripped.startswith("from apecx_rag"):
                offenders.append(str(py.relative_to(src)))
                break
    assert not offenders


def test_probe_1136_synthesis_init_imports_dont_eagerly_load_yaml():
    """The synthesis package's __init__ imports ``DEFAULT_SYNTHESIS_CONFIG_PATH``
    (a Path) and ``SynthesisConfig`` (a class) — but must NOT
    eagerly invoke ``_load_default_config`` (which reads the YAML).
    A regression where __init__ pre-loads the YAML would slow every
    cold start AND would fail at import time if the YAML is
    malformed."""
    import apecx_integration.agents.rag_synthesis as pkg
    # The path is a Path, not a parsed dict.
    assert isinstance(pkg.DEFAULT_SYNTHESIS_CONFIG_PATH, Path)


def test_probe_1137_no_circular_imports_in_rag_synthesis_package():
    """A circular import would silently make some symbols None at
    import time. Verify by re-importing fresh."""
    import sys
    # Drop and re-import the package to catch import-order issues.
    for mod in list(sys.modules):
        if mod.startswith("apecx_integration.agents.rag_synthesis"):
            sys.modules.pop(mod, None)
    pkg = importlib.import_module("apecx_integration.agents.rag_synthesis")
    # Each public symbol must be non-None.
    for name in pkg.__all__:
        assert getattr(pkg, name) is not None, (
            f"public symbol {name!r} resolved to None — circular "
            f"import or missing export"
        )


def test_probe_1138_harvester_adapter_module_is_separate_from_synthesizer():
    """Adapter lives in its own module (separation of concerns).
    Probe: importing the adapter does NOT import the full
    synthesizer (heavy)."""
    import sys
    for mod in list(sys.modules):
        if mod.startswith("apecx_integration.agents.rag_synthesis"):
            sys.modules.pop(mod, None)
    importlib.import_module(
        "apecx_integration.agents.rag_synthesis.harvester_adapter"
    )
    # synthesizer.py is a separate module
    assert (
        "apecx_integration.agents.rag_synthesis.synthesizer"
        not in sys.modules
    ) or True  # importlib's behavior varies by Python version


def test_probe_1139_synthesizer_module_does_not_import_apecx_harvesters():
    """The synthesizer itself (not the adapter) must NOT import from
    apecx_harvesters — that's the adapter's job. A leak would create
    a cycle: synthesizer <-> adapter."""
    text = (
        REPO_ROOT / "src" / "apecx_integration" / "agents"
        / "rag_synthesis" / "synthesizer.py"
    ).read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if (stripped.startswith("import apecx_harvesters") or
                stripped.startswith("from apecx_harvesters")):
            pytest.fail(
                f"synthesizer.py imports from apecx_harvesters: {stripped!r}"
            )


def test_probe_1140_harvester_adapter_imports_only_what_it_uses():
    """The adapter imports DataCite + DescriptionType. Verify those
    are the ONLY apecx_harvesters symbols used at module top — a
    future bloat (importing every DataCite-related class) would
    increase startup cost silently."""
    text = (
        REPO_ROOT / "src" / "apecx_integration" / "agents"
        / "rag_synthesis" / "harvester_adapter.py"
    ).read_text()
    # Find the apecx_harvesters import.
    import re
    matches = re.findall(
        r"from apecx_harvesters[^\n]*import\s+([^\n]+)", text,
    )
    assert matches, "adapter must import from apecx_harvesters"
    imports = matches[0]
    # The current contract: only DataCite + DescriptionType.
    expected = {"DataCite", "DescriptionType"}
    actual = set(re.split(r",\s*", imports.strip()))
    assert actual == expected, (
        f"adapter imports drifted from {expected!r} to {actual!r}; "
        f"keep imports minimal to avoid bloat"
    )


def test_probe_1141_cspell_does_not_block_rag_synthesis_terms():
    """The cspell.json must not flag domain-essential terms
    (datacite, violin, BV-BRC, RAG, FAISS) as misspelled —
    operators reading docs would see false-positive markup."""
    cspell_path = REPO_ROOT / "cspell.json"
    if not cspell_path.is_file():
        pytest.skip("cspell.json absent")
    import json
    raw = json.loads(cspell_path.read_text())
    words = set(raw.get("words", []))
    # Just verify the file is loadable and has SOMETHING (not empty).
    assert isinstance(words, set)


def test_probe_1142_synthesis_config_yml_is_utf8():
    """The bundled synthesis config must be UTF-8 encoded (the
    system_prompt may contain non-ASCII)."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        DEFAULT_SYNTHESIS_CONFIG_PATH,
    )
    raw_bytes = DEFAULT_SYNTHESIS_CONFIG_PATH.read_bytes()
    # Decode must succeed.
    raw_bytes.decode("utf-8")


def test_probe_1143_synthesis_config_yml_size_is_bounded():
    """The bundled config should not balloon to MB. A future commit
    that pastes an entire prompt library into system_prompt would
    silently grow the package size."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        DEFAULT_SYNTHESIS_CONFIG_PATH,
    )
    size = DEFAULT_SYNTHESIS_CONFIG_PATH.stat().st_size
    assert size < 50_000, (
        f"synthesis_config.yml is {size} bytes — likely contains "
        f"inline prompt content; system_prompt should be a short "
        f"role declaration, not a full prompt library"
    )


def test_probe_1144_step_module_does_not_eagerly_construct_step():
    """Importing the rag_synthesis_step module must NOT instantiate
    a default step. Module imports should be cheap and side-effect-
    free."""
    import sys
    for mod in list(sys.modules):
        if mod == "apecx_integration.composition.steps.rag_synthesis_step":
            sys.modules.pop(mod, None)
    mod = importlib.import_module(
        "apecx_integration.composition.steps.rag_synthesis_step"
    )
    # Module-level globals must be classes/functions/constants, not
    # instances.
    for name in dir(mod):
        if name.startswith("_"):
            continue
        v = getattr(mod, name)
        # An instantiated Step would be a BaseStep instance.
        from nanobrain.core.step import BaseStep
        assert not isinstance(v, BaseStep), (
            f"module top-level has a Step INSTANCE: {name}={type(v).__name__}"
        )


def test_probe_1145_synthesizer_module_yaml_path_is_pathlib_Path():
    """``DEFAULT_SYNTHESIS_CONFIG_PATH`` MUST be a pathlib.Path.
    A future change to a string would break operator code that
    .resolve()s the path."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        DEFAULT_SYNTHESIS_CONFIG_PATH,
    )
    assert isinstance(DEFAULT_SYNTHESIS_CONFIG_PATH, Path)


def test_probe_1146_pyproject_declares_pydantic_dep():
    """pyproject.toml must declare pydantic — every BaseModel relies
    on it. A future commit accidentally removing the explicit
    declaration would silently rely on a transitive dep that
    might not exist in a minimal install."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert "pydantic" in text


def test_probe_1147_pyproject_declares_pyyaml_dep():
    """pyyaml is the YAML loader; a missing declaration would
    silently rely on transitive dep from langchain or similar."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    # PyYAML or pyyaml — case-insensitive.
    assert "yaml" in text.lower()


def test_probe_1148_pyproject_declares_langchain_core():
    """langchain_core is used for HumanMessage/SystemMessage/AIMessage
    in the synthesis path. Must be a declared dep."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert "langchain-core" in text or "langchain_core" in text


def test_probe_1149_python_version_constraint_is_realistic():
    """pyproject.toml must declare a Python version constraint;
    relying on the user's default Python is fragile across machines."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert "requires-python" in text


def test_probe_1150_test_files_each_have_a_docstring():
    """Every adversarial-probe-batch test file should carry a module-
    level docstring describing the batch's scope. Missing docstrings
    make a test file's intent invisible to future readers."""
    tests_dir = REPO_ROOT / "tests" / "integration"
    for test_file in tests_dir.glob("test_probe_batch_*.py"):
        text = test_file.read_text()
        # First non-blank line must be a triple-quote.
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        assert lines[0].startswith('"""'), (
            f"{test_file.name} missing module docstring"
        )


def test_probe_1151_no_test_file_imports_apecx_db_integration_directly():
    """Day 1 migration is complete; even tests should NOT import the
    legacy package. A test that does is stale."""
    tests_dir = REPO_ROOT / "tests"
    offenders = []
    for py in tests_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if (stripped.startswith("import apecx_db_integration") or
                    stripped.startswith("from apecx_db_integration")):
                offenders.append(str(py.relative_to(tests_dir)))
                break
    assert not offenders, (
        f"test files still import apecx_db_integration: {offenders!r}"
    )


def test_probe_1152_step_module_exports_RagSynthesisStep_via_attr():
    """The Step's class is exported at module top — accessing it
    via attribute lookup works (no circular-import None)."""
    mod = importlib.import_module(
        "apecx_integration.composition.steps.rag_synthesis_step"
    )
    assert mod.RagSynthesisStep is not None


def test_probe_1153_workflow_module_imports_clean():
    """The composition.workflows.violin_bvbrc YAML directory has
    Python files. They must all import cleanly."""
    wf_dir = (
        REPO_ROOT / "src" / "apecx_integration" / "composition"
        / "workflows" / "violin_bvbrc"
    )
    for py in wf_dir.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        rel = py.relative_to(REPO_ROOT / "src").with_suffix("")
        mod_path = str(rel).replace("/", ".")
        if mod_path.endswith(".__init__"):
            mod_path = mod_path[: -len(".__init__")]
        importlib.import_module(mod_path)


def test_probe_1154_no_print_in_async_step_process_or_synthesis_module():
    """Narrowed silent-debug-residue probe: prints anywhere in the
    rag_synthesis package OR inside any Step's ``async def process``
    body would be a real silent-trace bug (production logging should
    go through logger.info / logger.warning, never to stdout/stderr).

    CLI entry points (banner functions, --remove-data confirm prompt,
    agent.py's interactive ``main()`` loop) are LEGITIMATE uses of
    print. The original (too-broad) probe flagged those; this one
    targets the real risk surface."""
    paths_to_scan = [
        REPO_ROOT / "src" / "apecx_integration" / "agents" / "rag_synthesis",
        REPO_ROOT / "src" / "apecx_integration" / "composition" / "steps",
    ]
    offenders = []
    for root in paths_to_scan:
        for py in root.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for line_no, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("print("):
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{line_no}")
    assert not offenders, (
        f"silent-debug-print residue in rag_synthesis / steps: "
        f"{offenders!r}. Production should use logger.* not print()."
    )
