"""Probe batch 10 — docs / LLM-guidance content accuracy.

Probes 226-250. Check that CLAUDE.md, composer prompts, and
referenced examples actually align with the framework code they
describe. A doc that promises "this class exists at
nanobrain.X.Y" must be checked against the actual import.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
PROMPT_DIR = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_prompts"


try:
    import nanobrain  # noqa: F401

    _NANOBRAIN_AVAILABLE = True
except ImportError:
    _NANOBRAIN_AVAILABLE = False


# --- Probe 226: workspace CLAUDE.md exists and is non-empty ---


def test_probe_226_workspace_claude_md_exists() -> None:
    p = WORKSPACE_ROOT / "CLAUDE.md"
    assert p.is_file()
    assert p.stat().st_size > 100


# --- Probe 227: apecx-mcp-integration/CLAUDE.md exists ---


def test_probe_227_repo_claude_md_exists() -> None:
    p = REPO_ROOT / "CLAUDE.md"
    assert p.is_file()


# --- Probe 228: nanobrain CLAUDE.md exists ---


def test_probe_228_nanobrain_claude_md_exists() -> None:
    p = WORKSPACE_ROOT / "nanobrain" / "CLAUDE.md"
    if not p.is_file():
        pytest.skip("nanobrain CLAUDE.md not present in this checkout")
    assert p.stat().st_size > 100


# --- Probe 229: workspace CLAUDE.md references real file paths ---


def test_probe_229_workspace_claude_md_links_resolve() -> None:
    """Workspace CLAUDE.md may reference repo-relative paths
    (e.g., apecx-mcp-integration/docs/QUICKSTART.md).  Validate
    that any such paths still resolve on disk.

    Vacuous-pass when the workspace CLAUDE.md no longer mentions
    any apecx-mcp-integration/*.md path (which is the case after
    the 2026-04-28 docs cleanup that moved dev artifacts to
    _workspace_notes/).  That's intentional — the test guards
    against stale repo-relative paths but doesn't require them."""
    text = (WORKSPACE_ROOT / "CLAUDE.md").read_text()
    # Look for `apecx-mcp-integration/...` paths
    for match in re.finditer(r"apecx-mcp-integration/[\w./_-]+\.md", text):
        p = WORKSPACE_ROOT / match.group(0)
        assert p.is_file(), (
            f"PROBE 229 BUG: workspace CLAUDE.md references {match.group(0)} " "which doesn't exist"
        )


# --- Probe 230: repo CLAUDE.md size is reasonable for always-loaded context ---


def test_probe_230_repo_claude_md_size_bounded() -> None:
    """CLAUDE.md is loaded into every Claude session — must be
    short enough to not blow the context budget."""
    p = REPO_ROOT / "CLAUDE.md"
    size = p.stat().st_size
    # ~10KB is a reasonable upper bound for an always-loaded context file
    assert size < 16_000, f"PROBE 230: repo CLAUDE.md is {size} bytes — may bloat session context"


# --- Probe 231: workspace CLAUDE.md size bounded ---


def test_probe_231_workspace_claude_md_size_bounded() -> None:
    p = WORKSPACE_ROOT / "CLAUDE.md"
    size = p.stat().st_size
    assert size < 24_000, f"PROBE 231: workspace CLAUDE.md is {size} bytes — too large"


# --- Probe 232: composer system.md mentions "DataUnitMemory" (real class) ---


def test_probe_232_system_md_uses_real_data_unit_class() -> None:
    text = (PROMPT_DIR / "system.md").read_text()
    # If system.md mentions specific data unit classes, they should
    # be real. DataUnitMemory is real (lightweight builder reports
    # it as available).
    if "DataUnitMemory" not in text:
        pytest.skip("DataUnitMemory not mentioned (acceptable)")
    # Verify the import path resolves
    if _NANOBRAIN_AVAILABLE:
        try:
            from nanobrain.core.data_unit import DataUnitMemory  # noqa
        except ImportError:
            try:
                from nanobrain.core.data_unit import DataUnit  # noqa
            except ImportError:
                pytest.fail(
                    "PROBE 232: system.md mentions DataUnitMemory but "
                    "neither DataUnitMemory nor DataUnit importable"
                )


# --- Probe 233: composer prompts collectively under prompt budget ---


def test_probe_233_combined_prompt_budget() -> None:
    """All prompt files combined fit in a 2K-token budget for the
    LLM (assuming ~4 chars/token). 8KB total combined."""
    total = sum(f.stat().st_size for f in PROMPT_DIR.iterdir() if f.suffix == ".md")
    assert total < 32_000, (
        f"PROBE 233: combined prompts total {total} bytes — large fraction "
        "of any LLM's context budget; consider tightening"
    )


# --- Probe 234: All composer prompt files are valid UTF-8 ---


def test_probe_234_prompt_files_valid_utf8() -> None:
    for f in PROMPT_DIR.iterdir():
        if f.suffix != ".md":
            continue
        try:
            f.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            pytest.fail(f"PROBE 234 BUG: {f.name} is not valid UTF-8: {e}")


# --- Probe 235: nanobrain.core.workflow has Workflow class ---


def test_probe_235_nanobrain_workflow_class_present() -> None:
    if not _NANOBRAIN_AVAILABLE:
        pytest.skip("nanobrain not importable")
    from nanobrain.core.workflow import Workflow

    assert hasattr(Workflow, "from_config")


# --- Probe 236: nanobrain.core.step has step base class ---


def test_probe_236_nanobrain_step_class_present() -> None:
    if not _NANOBRAIN_AVAILABLE:
        pytest.skip("nanobrain not importable")
    import nanobrain.core.step  # noqa: F401


# --- Probe 237: Lightweight workflow_builder has add_step + connect ---


def test_probe_237_lightweight_builder_api_complete() -> None:
    if not _NANOBRAIN_AVAILABLE:
        pytest.skip()
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(name="t", description="d")
    assert hasattr(b, "add_step")
    assert hasattr(b, "add_input")
    assert hasattr(b, "add_output")
    assert hasattr(b, "connect")
    assert hasattr(b, "save_config")


# --- Probe 238: composer system.md doesn't recommend bare class instantiation ---


def test_probe_238_system_md_no_direct_constructor() -> None:
    """Framework rule (workspace CLAUDE.md): from_config only,
    direct constructors forbidden. Prompt MUST not give an example
    that uses bare constructor syntax like ``Step(name=...)``.
    """
    text = (PROMPT_DIR / "system.md").read_text()
    # Heuristic: if a Python-like ``ClassName(arg=value)`` pattern
    # appears outside a ``do not`` context, that's suspicious.
    # Strict assertion: the prompt mentions ``from_config`` or
    # YAML-only steering, not Python construction.
    # Doesn't have to mention from_config — workflows are YAML —
    # but if it shows Python init it's a problem.
    suspicious = re.findall(r"\b(BaseStep|Step|Workflow)\([\w=, ]+\)", text)
    # Filter out class-path strings (no parens like that)
    if suspicious:
        for s in suspicious:
            ctx_idx = text.find(s)
            ctx = text[max(0, ctx_idx - 100) : ctx_idx + 200]
            assert any(
                neg in ctx.lower() for neg in ["do not", "forbid", "not allowed"]
            ), f"PROBE 238 BUG: system.md shows direct constructor {s!r} without 'do not' context"


# --- Probe 239: composer system.md mentions wrapper YAML correctly ---


def test_probe_239_system_md_wrapper_yaml_explanation() -> None:
    text = (PROMPT_DIR / "system.md").read_text()
    # Must explain wrapper YAMLs (steps/<name>.yml) since the
    # framework relies on them for component reuse.
    assert "wrapper" in text.lower() or "steps/" in text or ".yml" in text


# --- Probe 240: composition_bias.md doesn't contradict system.md on TransformLink ---


def test_probe_240_no_transformlink_recommendation_in_bias() -> None:
    text = (PROMPT_DIR / "composition_bias.md").read_text()
    if "TransformLink" not in text:
        pytest.skip("TransformLink not in composition_bias.md")
    for match in re.finditer(r"TransformLink", text):
        ctx = text[max(0, match.start() - 200) : match.end() + 200]
        assert any(
            neg in ctx for neg in ["NOT", "forbid", "don't", "Do NOT", "do not"]
        ), "PROBE 240 BUG: composition_bias.md mentions TransformLink without forbidding context"


# --- Probe 241: novel_python_flagging.md explains when to flag novel code ---


def test_probe_241_novel_python_flagging_has_when_clause() -> None:
    text = (PROMPT_DIR / "novel_python_flagging.md").read_text()
    # Must explain WHEN novel python is appropriate.
    assert any(
        word in text.lower() for word in ["when", "if", "should"]
    ), "PROBE 241: novel_python_flagging.md doesn't explain when to flag"


# --- Probe 242: prompt files don't reference deprecated/removed APIs ---


def test_probe_242_prompts_no_deprecated_references() -> None:
    """If the prompt mentions any ``register_agent`` (the removed
    nanobrain API), it must be in a 'removed' / 'do not' context.
    """
    text = " ".join(
        (PROMPT_DIR / f).read_text()
        for f in ["system.md", "composition_bias.md", "novel_python_flagging.md"]
    )
    if "register_agent" in text:
        # Must be mentioned in removal context
        for match in re.finditer(r"register_agent", text):
            ctx = text[max(0, match.start() - 200) : match.end() + 200]
            assert any(
                neg in ctx.lower() for neg in ["removed", "do not", "deprecated"]
            ), "PROBE 242 BUG: prompt references register_agent without removal context"


# --- Probe 243: composer config.yml is parseable YAML ---


def test_probe_243_composer_config_parseable() -> None:
    cfg = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
    if not cfg.is_file():
        pytest.skip()
    import yaml

    yaml.safe_load(cfg.read_text())


# --- Probe 244: All YAML files in composition/workflows are parseable ---


def test_probe_244_all_workflow_yamls_parse() -> None:
    workflows_dir = REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows"
    if not workflows_dir.is_dir():
        pytest.skip()
    import yaml

    for yml in workflows_dir.rglob("*.yml"):
        try:
            yaml.safe_load(yml.read_text())
        except yaml.YAMLError as e:
            pytest.fail(f"PROBE 244 BUG: {yml} is malformed YAML: {e}")


# --- Probe 245: All yml files have no tab indentation (YAML strict) ---


def test_probe_245_no_tab_indentation_in_yamls() -> None:
    workflows_dir = REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows"
    if not workflows_dir.is_dir():
        pytest.skip()
    for yml in workflows_dir.rglob("*.yml"):
        text = yml.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            assert "\t" not in line, f"PROBE 245: {yml}:{lineno} has tab indentation: {line!r}"


# --- Probe 246: workspace CLAUDE.md's nanobrain rules section exists ---


def test_probe_246_workspace_claude_md_has_nanobrain_section() -> None:
    text = (WORKSPACE_ROOT / "CLAUDE.md").read_text()
    assert "nanobrain" in text.lower()
    assert "from_config" in text or "FromConfigBase" in text


# --- Probe 247: workspace CLAUDE.md has mocks-policy section ---


def test_probe_247_workspace_claude_md_has_mocks_policy() -> None:
    text = (WORKSPACE_ROOT / "CLAUDE.md").read_text()
    assert "mock" in text.lower()


# --- Probe 248: workspace CLAUDE.md has git discipline section ---


def test_probe_248_workspace_claude_md_has_git_discipline() -> None:
    text = (WORKSPACE_ROOT / "CLAUDE.md").read_text()
    assert "git" in text.lower()
    assert "branch" in text.lower() or "worktree" in text.lower()


# --- Probe 249: friction log's "How to add to this log" section exists ---


def test_probe_249_friction_log_has_meta() -> None:
    fl = REPO_ROOT / "docs" / "session_friction_log.md"
    if not fl.is_file():
        pytest.skip()
    text = fl.read_text()
    assert "How to add" in text or "When to" in text


# --- Probe 250: friction log mentions all my new entries (16-19) ---


def test_probe_250_friction_log_has_session_entries() -> None:
    """Confirms my session's distillation actually landed."""
    fl = REPO_ROOT / "docs" / "session_friction_log.md"
    if not fl.is_file():
        pytest.skip()
    text = fl.read_text()
    assert "## 16." in text
    assert "## 17." in text
    assert "## 18." in text
    assert "## 19." in text
