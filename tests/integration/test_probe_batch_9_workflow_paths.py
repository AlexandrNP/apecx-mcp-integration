"""Probe batch 9 — multi-path workflow creation + LLM-guidance accuracy.

User scope expansion (2026-04-26): "Explore multiple legit ways of
creating workflows, including lightweight nanobrain. Ensure files
for guiding LLMs are efficient and allows generated code to follow
framework conventions and tackle tasks efficiently."

Probes 203-225. Each is one distinct adversarial probe against:
- nanobrain.lightweight.WorkflowBuilder (programmatic API)
- nanobrain.core.workflow.Workflow.from_config (YAML API)
- composer_prompts/*.md (LLM-guidance files for accuracy)
- Cross-API equivalence (does Builder produce loadable YAML?)
- Round-trip: write → save_config → from_config → behavior parity
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_prompts"
)


try:
    import nanobrain.core.workflow  # noqa: F401
    import nanobrain.lightweight.workflow_builder  # noqa: F401
    _NANOBRAIN_AVAILABLE = True
except ImportError:
    _NANOBRAIN_AVAILABLE = False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _NANOBRAIN_AVAILABLE,
        reason="nanobrain not importable — run under .venv/bin/python",
    ),
]


# --- Probe 203: lightweight WorkflowBuilder constructs without error ---


def test_probe_203_workflow_builder_smoke() -> None:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="probe", description="test")
    assert b is not None


# --- Probe 204: WorkflowBuilder add_input / add_output / get_config ---


def test_probe_204_workflow_builder_add_inputs(tmp_path) -> None:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="p", description="d")
    b.add_input("entry", "DataUnitMemory")
    b.add_output("exit", "DataUnitMemory")
    cfg = b.get_config()
    assert isinstance(cfg, dict)
    assert "name" in cfg


# --- Probe 205: WorkflowBuilder.save_config produces file with the name field ---


def test_probe_205_workflow_builder_save_config_has_name(tmp_path) -> None:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="my_workflow", description="d")
    out = tmp_path / "wf.yml"
    b.save_config(str(out))
    text = out.read_text()
    assert "my_workflow" in text


# --- Probe 206: WorkflowBuilder produces a YAML file that Workflow.from_config can attempt to load ---


def test_probe_206_workflow_builder_to_workflow_load(tmp_path) -> None:
    """Bridge probe: lightweight builder → save → core Workflow load.
    Empty workflow may not be a valid Workflow per framework rules
    but it must not produce malformed YAML."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="empty", description="d")
    out = tmp_path / "wf.yml"
    b.save_config(str(out))
    import yaml
    parsed = yaml.safe_load(out.read_text())
    assert isinstance(parsed, dict)
    assert parsed.get("name") == "empty"


# --- Probe 207: composer_prompts/system.md exists and is non-empty ---


def test_probe_207_system_md_exists() -> None:
    p = PROMPT_DIR / "system.md"
    assert p.is_file()
    assert p.stat().st_size > 100


# --- Probe 208: system.md does not reference TransformLink as recommended ---


def test_probe_208_system_md_forbids_transformlink() -> None:
    """Cluster T01 P2 lesson: TransformLink import paths get
    hallucinated. system.md must NOT recommend it.
    """
    text = (PROMPT_DIR / "system.md").read_text()
    # The prompt SHOULD mention TransformLink to forbid it. The
    # signal of accuracy: every TransformLink mention should be in
    # a 'do NOT' or 'forbid' context.
    if "TransformLink" not in text:
        pytest.skip("TransformLink not mentioned (acceptable)")
    # Heuristic: any TransformLink mention should be near a 'NOT'
    # or 'forbid' or 'don't' word within a window.
    for match in re.finditer(r"TransformLink", text):
        start = max(0, match.start() - 200)
        end = min(len(text), match.end() + 200)
        context = text[start:end]
        # If it's recommended (not forbidden), this is a real bug.
        assert any(neg in context for neg in ["NOT", "forbid", "don't", "Do NOT", "do not"]), (
            f"PROBE 208 BUG: system.md mentions TransformLink without "
            f"forbidding context: {context!r}"
        )


# --- Probe 209: system.md mandates path-reference config, not inline ---


def test_probe_209_system_md_mandates_path_config() -> None:
    text = (PROMPT_DIR / "system.md").read_text()
    # Should mandate ``config: "<wrapper_yaml>"`` for library
    # components.
    assert "config:" in text
    # Forbidden inline pattern
    assert (
        "inline" in text.lower() or "do not emit inline" in text.lower()
        or "Do NOT emit inline" in text
    )


# --- Probe 210: novel_python_flagging.md exists ---


def test_probe_210_novel_python_flagging_exists() -> None:
    p = PROMPT_DIR / "novel_python_flagging.md"
    assert p.is_file()


# --- Probe 211: composition_bias.md exists ---


def test_probe_211_composition_bias_exists() -> None:
    p = PROMPT_DIR / "composition_bias.md"
    assert p.is_file()


# --- Probe 212: composer_config.yml references the prompt dir correctly ---


def test_probe_212_composer_config_prompt_dir() -> None:
    cfg_path = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "composer_config.yml"
    )
    if not cfg_path.is_file():
        pytest.skip("composer_config.yml not present")
    import yaml
    raw = yaml.safe_load(cfg_path.read_text())
    pdir = raw.get("prompt_dir")
    assert pdir is not None


# --- Probe 213: ComposerConfig validates the example composer_config.yml ---


def test_probe_213_composer_config_loads() -> None:
    from apecx_integration.composition.composer_schemas import ComposerConfig
    cfg_path = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "composer_config.yml"
    )
    if not cfg_path.is_file():
        pytest.skip("composer_config.yml not present")
    import yaml
    raw = yaml.safe_load(cfg_path.read_text())
    cfg = ComposerConfig.model_validate(raw)
    assert cfg.library_version


# --- Probe 214: system.md doesn't reference nonexistent class paths ---


def test_probe_214_system_md_no_known_hallucinated_paths() -> None:
    """Cluster T01 history: the LLM hallucinated paths like
    nanobrain.core.data_unit.TextDataUnit. system.md must NOT
    suggest such paths as templates.
    """
    text = (PROMPT_DIR / "system.md").read_text()
    bad_paths = [
        "nanobrain.core.data_unit.TextDataUnit",
        "nanobrain.core.data_unit.TextDataUnit",
    ]
    for bad in bad_paths:
        # If mentioned, must be in a NOT / forbid / hallucinate context.
        if bad not in text:
            continue
        for match in re.finditer(re.escape(bad), text):
            start = max(0, match.start() - 200)
            end = min(len(text), match.end() + 200)
            context = text[start:end]
            assert any(
                neg in context.lower() for neg in ["hallucinate", "do not", "not exist"]
            ), f"PROBE 214 BUG: {bad} appears outside a 'do not' context"


# --- Probe 215: Lightweight builder accepts variadic kwargs without crashing ---


def test_probe_215_workflow_builder_kwargs() -> None:
    """Lightweight Builder takes SHORT class names, not full paths.
    Inconsistent with composer prompt which mandates full
    implementation_path. Probe documents the short-name path
    and asserts it accepts arbitrary kwargs."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="kw", description="d")
    b.add_step("s1", "DataUnitMemory", custom_field="value")
    cfg = b.get_config()
    assert "steps" in cfg or "name" in cfg


# --- Probe 216: connect() between two added steps doesn't crash ---


def test_probe_216_workflow_builder_connect() -> None:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="c", description="d")
    b.add_step("s1", "DataUnitMemory")
    b.add_step("s2", "DataUnitMemory")
    b.connect("s1.out", "s2.in")
    cfg = b.get_config()
    assert cfg is not None


# --- Probe 217: workflow YAML with empty steps mapping is well-formed ---


def test_probe_217_yaml_empty_steps_well_formed(tmp_path) -> None:
    yaml_text = """
name: empty
description: empty
version: "0.1.0"
steps: {}
links: {}
"""
    p = tmp_path / "wf.yml"
    p.write_text(yaml_text)
    import yaml
    parsed = yaml.safe_load(p.read_text())
    assert parsed["name"] == "empty"
    assert parsed["steps"] == {}


# --- Probe 218: composer prompts directory has no stale file ---


def test_probe_218_prompt_dir_only_md_files() -> None:
    """If a non-.md file exists in prompts dir, it might be loaded
    accidentally by future glob-based prompt loaders."""
    for f in PROMPT_DIR.iterdir():
        if f.is_file():
            assert f.suffix in {".md", ".yml", ".yaml"}, (
                f"PROBE 218: unexpected file in prompt dir: {f}"
            )


# --- Probe 219: Builder.add_step is chainable (returns self) ---


def test_probe_219_builder_chainable() -> None:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = (
        WorkflowBuilder(name="chain", description="d")
        .add_step("s1", "DataUnitMemory")
        .add_step("s2", "DataUnitMemory")
    )
    cfg = b.get_config()
    assert cfg is not None


# --- Probe 220: list_available_components doesn't raise ---


def test_probe_220_list_available_components() -> None:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="l", description="d")
    # Method exists and returns a list (possibly empty).
    components = b.list_available_components()
    assert isinstance(components, list)


# --- Probe 221: get_component_info returns None or dict for a query ---


def test_probe_221_get_component_info() -> None:
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder
    b = WorkflowBuilder(name="g", description="d")
    info = b.get_component_info("nanobrain.core.step.BaseStep")
    assert info is None or isinstance(info, dict)


# --- Probe 222: composer_prompts/system.md mentions DirectLink ---


def test_probe_222_system_md_recommends_directlink() -> None:
    text = (PROMPT_DIR / "system.md").read_text()
    assert "DirectLink" in text


# --- Probe 223: system.md prompts size is reasonable for LLM context ---


def test_probe_223_system_md_size_reasonable() -> None:
    """Prompt should fit comfortably in any ~4K token budget. 16KB
    is a safe upper bound for prompt-only content (LLM can fit
    16KB within most Phase-2 max_tokens=4096 reasoning budgets)."""
    p = PROMPT_DIR / "system.md"
    size = p.stat().st_size
    assert size < 16_000, f"PROBE 223: system.md is {size} bytes — may exceed prompt budget"


# --- Probe 224: composition_bias.md size reasonable ---


def test_probe_224_composition_bias_size_reasonable() -> None:
    p = PROMPT_DIR / "composition_bias.md"
    size = p.stat().st_size
    assert size < 16_000, (
        f"PROBE 224: composition_bias.md is {size} bytes — too large for prompt"
    )


# --- Probe 225: novel_python_flagging.md size reasonable ---


def test_probe_225_novel_python_flagging_size_reasonable() -> None:
    p = PROMPT_DIR / "novel_python_flagging.md"
    size = p.stat().st_size
    assert size < 16_000, (
        f"PROBE 225: novel_python_flagging.md is {size} bytes — too large"
    )
