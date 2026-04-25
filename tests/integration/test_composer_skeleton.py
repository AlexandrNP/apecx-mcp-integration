"""T-COMP Phase 1 — skeleton loadability tests.

Per spec §6 P1 exit criterion: ``Composer.from_config(default_config)``
passes; ``compose()`` is not yet callable.

No LLM, no RAG, no ArtifactStore. Those integrations land in P2–P4.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.composer import (
    REQUIRED_PROMPT_FILES,
    Composer,
    ComposerConfigurationError,
)
from apecx_integration.composition.composer_schemas import ComposerConfig

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)
PROMPT_DIR = DEFAULT_CONFIG.parent / "composer_prompts"


# ---------------------------------------------------------------------------
# from_config loadability — the Phase-1 exit criterion
# ---------------------------------------------------------------------------

def test_default_composer_config_loads():
    """Spec §6 P1: the bundled sample config loads without raising."""
    composer = Composer.from_config(DEFAULT_CONFIG)
    assert isinstance(composer.config, ComposerConfig)
    assert composer.config.library_version == "0.1.0-dev"


def test_all_three_placeholder_prompts_are_loaded():
    """AC6 enforcement: the prompt dir claims all three required prompt
    slots — Phase 2 replaces the text; Phase 1 pins the slot count.
    """
    composer = Composer.from_config(DEFAULT_CONFIG)
    loaded_keys = set(composer.prompts.keys())
    expected_keys = {p.removesuffix(".md") for p in REQUIRED_PROMPT_FILES}
    assert loaded_keys == expected_keys
    # Each prompt has non-empty content, even if it's placeholder text.
    for key, text in composer.prompts.items():
        assert text.strip(), f"prompt {key!r} is empty"


def test_no_inline_prompt_text_in_composer_source():
    """AC6 the other direction: grep ``composer.py`` for likely-inline
    prompt strings. Expectation: zero hits.

    The grep is deliberately forgiving — we check for the literal text
    of one of the placeholder prompts. If a future author copies prompt
    text into Python source, this test fails with a clear message.
    """
    composer_py = Path(
        REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer.py"
    ).read_text()
    # Markers are distinctive strings from the CURRENT prompt files.
    # If a future author copy-pastes prompt text into composer.py, these
    # substrings will appear in the Python source and the test fires.
    for marker in (
        "You are a workflow composer for the APECx nanobrain",
        "Prefer composition over generation",
        "If — and only if — you emit novel Python",
    ):
        assert marker not in composer_py, (
            f"found inline prompt text in composer.py: {marker!r}. "
            "AC6 says all prompt text lives in composer_prompts/*.md, "
            "not in Python source."
        )


# ---------------------------------------------------------------------------
# compose() validates the prompt shape even before calling the LLM
# ---------------------------------------------------------------------------

def test_compose_rejects_empty_prompt():
    """compose() must guard against empty prompts before invoking the
    LLM — an empty prompt wastes a round-trip and confuses the model.
    """
    composer = Composer.from_config(DEFAULT_CONFIG)
    with pytest.raises(ValueError, match="non-empty prompt"):
        asyncio.run(composer.compose(""))
    with pytest.raises(ValueError, match="non-empty prompt"):
        asyncio.run(composer.compose("   "))


# ---------------------------------------------------------------------------
# Error paths — config validation
# ---------------------------------------------------------------------------

def test_missing_config_file_raises_cleanly(tmp_path):
    bogus = tmp_path / "does_not_exist.yml"
    with pytest.raises(ComposerConfigurationError, match="not found"):
        Composer.from_config(bogus)


def test_non_mapping_config_raises_cleanly(tmp_path):
    """A YAML file that's a list or scalar at the top level should fail
    with a clear message, not a cryptic pydantic error.
    """
    p = tmp_path / "scalar.yml"
    p.write_text("just a string\n")
    with pytest.raises(ComposerConfigurationError, match="YAML mapping"):
        Composer.from_config(p)


def test_missing_prompt_dir_field_raises_cleanly(tmp_path):
    p = tmp_path / "no_prompt_dir.yml"
    p.write_text("library_version: '0.1.0'\n")
    with pytest.raises(ComposerConfigurationError, match="prompt_dir"):
        Composer.from_config(p)


def test_missing_required_prompt_file_raises_cleanly(tmp_path):
    """prompt_dir exists but lacks one of the required files."""
    prompt_dir = tmp_path / "partial_prompts"
    prompt_dir.mkdir()
    (prompt_dir / "system.md").write_text("# system")
    (prompt_dir / "composition_bias.md").write_text("# bias")
    # DELIBERATELY MISSING: novel_python_flagging.md

    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        "library_version: '0.1.0'\n"
        f"prompt_dir: '{prompt_dir}'\n"
    )
    with pytest.raises(ComposerConfigurationError, match="novel_python_flagging"):
        Composer.from_config(cfg)


def test_prompt_dir_is_resolved_relative_to_config_file(tmp_path):
    """A ``prompt_dir: composer_prompts`` in the YAML should resolve to
    ``<config_parent>/composer_prompts``, independent of CWD.
    """
    # Lay out a mini composer config tree in tmp_path.
    prompt_dir = tmp_path / "my_prompts"
    prompt_dir.mkdir()
    for fname in REQUIRED_PROMPT_FILES:
        (prompt_dir / fname).write_text(f"# {fname}")

    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        "library_version: '0.1.0'\n"
        "prompt_dir: 'my_prompts'\n"
    )

    composer = Composer.from_config(cfg)
    assert composer.config.prompt_dir == prompt_dir


def test_bad_temperature_range_raises_cleanly(tmp_path, monkeypatch):
    """Pydantic validation should surface via ComposerConfigurationError,
    not raw pydantic.ValidationError — error-message ergonomics for
    operators who aren't fluent in pydantic.

    Found 2026-04-25 (live-suite verification): pre-fix this test
    was test-isolation-broken. ``_apply_llm_env_overrides`` runs
    BEFORE pydantic validation and silently rewrites the YAML's
    temperature with the value of ``APECX_LLM_TEMPERATURE`` if
    that env var is set. So a developer running pytest in a shell
    that has the live-LLM recipe env vars exported (per
    ``CLAUDE.md`` "Live-LLM test recipe") would see this test pass
    with a bad temperature being silently corrected, masking the
    real validation gate. Explicitly delete the env var so the
    test always exercises the YAML-side validation path it
    documents.
    """
    monkeypatch.delenv("APECX_LLM_TEMPERATURE", raising=False)
    prompt_dir = tmp_path / "my_prompts"
    prompt_dir.mkdir()
    for fname in REQUIRED_PROMPT_FILES:
        (prompt_dir / fname).write_text(f"# {fname}")
    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        "library_version: '0.1.0'\n"
        f"prompt_dir: '{prompt_dir}'\n"
        "temperature: 99.0\n"  # out of pydantic range
    )
    with pytest.raises(ComposerConfigurationError, match="validation"):
        Composer.from_config(cfg)
