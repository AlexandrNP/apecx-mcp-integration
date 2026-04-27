"""Probe batch 26 — composer.py internals (probes 680-704).

The composer is the highest-leverage component in apecx-mcp-integration:
every workflow generation passes through ``Composer.compose()``. A
silent bug in its config loading, prompt assembly, env-var override,
or candidate rendering cascades into every workflow run.

This batch probes the pure-Python parts of composer.py that don't
require an actual LLM call:

  - Composer.from_config — config file resolution, error mapping,
    path-resolution-relative-to-config behavior.
  - _load_prompts — required prompt files contract.
  - _apply_llm_env_overrides — APECX_LLM_* env-var honoring.
  - _render_candidates / _build_user_prompt — internal-key
    redaction and candidate-block rendering.
  - compose() input validation — empty / non-string prompt rejection.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_PROMPTS = (
    _REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_prompts"
)


def _make_minimal_config_yaml(
    tmp_path: Path,
    *,
    extra: dict | None = None,
) -> Path:
    """Write a minimal ComposerConfig YAML and return its path. Uses
    the real composer_prompts dir so prompts load successfully."""
    import yaml as _yaml
    raw = {
        "library_version": "0.1.0-test",
        "prompt_dir": str(_REAL_PROMPTS),
    }
    if extra:
        raw.update(extra)
    p = tmp_path / "composer_config.yml"
    p.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Composer.from_config — probes 680-686
# ---------------------------------------------------------------------------


def test_probe_680_from_config_missing_file_raises(tmp_path) -> None:
    """A non-existent config file must raise ComposerConfigurationError
    with a clear message — not a generic FileNotFoundError that's
    indistinguishable from "permissions issue."""
    from apecx_integration.composition.composer import (
        Composer, ComposerConfigurationError,
    )
    with pytest.raises(ComposerConfigurationError, match="not found"):
        Composer.from_config(tmp_path / "missing.yml")


def test_probe_681_from_config_non_mapping_raises(tmp_path) -> None:
    """A config file whose YAML root is a list or scalar (operator
    mistake) must fail-fast at the composer layer, not later inside
    Pydantic with a confusing TypeError."""
    from apecx_integration.composition.composer import (
        Composer, ComposerConfigurationError,
    )
    p = tmp_path / "bad.yml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ComposerConfigurationError, match="mapping"):
        Composer.from_config(p)


def test_probe_682_from_config_missing_prompt_dir_raises(tmp_path) -> None:
    """prompt_dir is required; absence must produce a clear error
    that names the missing field."""
    import yaml as _yaml
    from apecx_integration.composition.composer import (
        Composer, ComposerConfigurationError,
    )
    p = tmp_path / "no_prompt_dir.yml"
    p.write_text(_yaml.safe_dump({"library_version": "0.1.0"}), encoding="utf-8")
    with pytest.raises(ComposerConfigurationError, match="prompt_dir"):
        Composer.from_config(p)


def test_probe_683_from_config_resolves_prompt_dir_relative(tmp_path) -> None:
    """A relative prompt_dir must resolve relative to the config file's
    directory — NOT the working directory. Composer is often loaded
    from a working dir different from the config's location."""
    import yaml as _yaml
    from apecx_integration.composition.composer import Composer
    # Place a prompts dir alongside the config and reference it relatively
    cfg_dir = tmp_path / "subdir"
    cfg_dir.mkdir()
    prompt_dir = cfg_dir / "prompts"
    prompt_dir.mkdir()
    for name in ("system.md", "composition_bias.md", "novel_python_flagging.md"):
        (prompt_dir / name).write_text("test prompt", encoding="utf-8")
    p = cfg_dir / "composer_config.yml"
    p.write_text(_yaml.safe_dump({
        "library_version": "0.1.0",
        "prompt_dir": "prompts",
    }), encoding="utf-8")
    composer = Composer.from_config(p)
    # Resolution must produce an absolute path under cfg_dir
    assert composer.config.prompt_dir == prompt_dir.resolve()


def test_probe_684_from_config_resolves_catalog_paths_relative(tmp_path) -> None:
    import yaml as _yaml
    from apecx_integration.composition.composer import Composer
    cfg_dir = tmp_path / "cdir"
    cfg_dir.mkdir()
    # Create a manifest file relative to the config
    (cfg_dir / "wf").mkdir()
    manifest = cfg_dir / "wf" / "manifest.yml"
    manifest.write_text(_yaml.safe_dump({
        "components": [{
            "step_id": "x", "step_name": "x", "class": "test.X",
            "rag_description": "test desc",
        }]
    }), encoding="utf-8")
    p = cfg_dir / "cfg.yml"
    p.write_text(_yaml.safe_dump({
        "library_version": "0.1.0",
        "prompt_dir": str(_REAL_PROMPTS),
        "component_catalog_paths": ["wf/manifest.yml"],
    }), encoding="utf-8")
    composer = Composer.from_config(p)
    # Resolution must point at the file under cfg_dir
    catalogs = list(composer.config.component_catalog_paths)
    assert len(catalogs) == 1
    assert catalogs[0] == manifest.resolve()


def test_probe_685_from_config_catalog_paths_must_be_list(tmp_path) -> None:
    import yaml as _yaml
    from apecx_integration.composition.composer import (
        Composer, ComposerConfigurationError,
    )
    p = tmp_path / "bad.yml"
    p.write_text(_yaml.safe_dump({
        "library_version": "0.1.0",
        "prompt_dir": str(_REAL_PROMPTS),
        "component_catalog_paths": "not_a_list.yml",
    }), encoding="utf-8")
    with pytest.raises(ComposerConfigurationError, match="component_catalog_paths"):
        Composer.from_config(p)


def test_probe_686_from_config_pydantic_failure_wraps(tmp_path) -> None:
    """A Pydantic validation failure (e.g. temperature=5.0 > le=2.0)
    must come back as ComposerConfigurationError so callers can
    distinguish operator misconfig from runtime LLM failures."""
    from apecx_integration.composition.composer import (
        Composer, ComposerConfigurationError,
    )
    p = _make_minimal_config_yaml(tmp_path, extra={"temperature": 5.0})
    with pytest.raises(ComposerConfigurationError, match="failed validation"):
        Composer.from_config(p)


# ---------------------------------------------------------------------------
# _load_prompts — probes 687-690
# ---------------------------------------------------------------------------


def test_probe_687_load_prompts_requires_all_three(tmp_path) -> None:
    """Missing any of the three required prompt files must
    fail-fast — silent fallback would let the composer ship with a
    half-loaded prompt."""
    from apecx_integration.composition.composer import (
        Composer, ComposerConfigurationError,
    )
    pdir = tmp_path / "p"
    pdir.mkdir()
    # Only system.md present
    (pdir / "system.md").write_text("x", encoding="utf-8")
    with pytest.raises(ComposerConfigurationError, match="missing required prompt files"):
        Composer._load_prompts(pdir)


def test_probe_688_load_prompts_rejects_non_directory(tmp_path) -> None:
    """A prompt_dir that's actually a file must fail-fast."""
    from apecx_integration.composition.composer import (
        Composer, ComposerConfigurationError,
    )
    not_a_dir = tmp_path / "not_a_dir.txt"
    not_a_dir.write_text("file, not dir", encoding="utf-8")
    with pytest.raises(ComposerConfigurationError, match="not a directory"):
        Composer._load_prompts(not_a_dir)


def test_probe_689_load_prompts_keys_by_stem(tmp_path) -> None:
    """The returned dict must be keyed by file stem (no .md
    extension) — the composer references prompts as ``system``,
    ``composition_bias``, ``novel_python_flagging``."""
    from apecx_integration.composition.composer import Composer
    pdir = tmp_path / "p"
    pdir.mkdir()
    for name in ("system.md", "composition_bias.md", "novel_python_flagging.md"):
        (pdir / name).write_text(f"content of {name}", encoding="utf-8")
    out = Composer._load_prompts(pdir)
    assert set(out.keys()) == {"system", "composition_bias", "novel_python_flagging"}
    assert out["system"] == "content of system.md"


def test_probe_690_required_prompt_files_locked() -> None:
    """The REQUIRED_PROMPT_FILES tuple is the contract every
    operator's prompt_dir must satisfy. Lock the exact set —
    a future addition or removal must come through here."""
    from apecx_integration.composition.composer import REQUIRED_PROMPT_FILES
    assert set(REQUIRED_PROMPT_FILES) == {
        "system.md", "composition_bias.md", "novel_python_flagging.md",
    }


# ---------------------------------------------------------------------------
# _apply_llm_env_overrides — probes 691-694
# ---------------------------------------------------------------------------


def test_probe_691_env_overrides_llm_model() -> None:
    """APECX_LLM_MODEL overrides llm_model in the raw config dict."""
    from apecx_integration.composition.composer import _apply_llm_env_overrides
    raw = {"llm_model": "yaml-default"}
    with patch.dict(os.environ, {"APECX_LLM_MODEL": "env-override"}):
        _apply_llm_env_overrides(raw)
    assert raw["llm_model"] == "env-override"


def test_probe_692_env_overrides_llm_base_url() -> None:
    from apecx_integration.composition.composer import _apply_llm_env_overrides
    raw = {"llm_base_url": "http://default"}
    with patch.dict(os.environ, {"APECX_LLM_BASE_URL": "http://override"}):
        _apply_llm_env_overrides(raw)
    assert raw["llm_base_url"] == "http://override"


def test_probe_693_env_overrides_temperature_coerces_float() -> None:
    """APECX_LLM_TEMPERATURE is a string in env vars; the override
    must coerce to float so Pydantic doesn't reject it."""
    from apecx_integration.composition.composer import _apply_llm_env_overrides
    raw = {"temperature": 0.0}
    with patch.dict(os.environ, {"APECX_LLM_TEMPERATURE": "0.7"}):
        _apply_llm_env_overrides(raw)
    assert raw["temperature"] == 0.7
    assert isinstance(raw["temperature"], float)


def test_probe_694_unset_env_leaves_yaml_value() -> None:
    """If APECX_LLM_* is unset, the raw dict's existing value must
    be preserved unchanged."""
    from apecx_integration.composition.composer import _apply_llm_env_overrides
    saved = {k: os.environ.pop(k, None) for k in
             ("APECX_LLM_MODEL", "APECX_LLM_BASE_URL",
              "APECX_LLM_TEMPERATURE", "APECX_LLM_MAX_TOKENS")}
    try:
        raw = {
            "llm_model": "yaml-value",
            "llm_base_url": "http://yaml-url",
            "temperature": 0.5,
            "max_tokens": 1024,
        }
        snapshot = dict(raw)
        _apply_llm_env_overrides(raw)
        assert raw == snapshot
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# _render_candidates + _build_user_prompt — probes 695-699
# ---------------------------------------------------------------------------


def test_probe_695_render_candidates_empty() -> None:
    """An empty hits list must produce an empty string — the
    caller's _build_user_prompt branches on this to substitute the
    "no matching components" message."""
    from apecx_integration.composition.composer import _render_candidates
    assert _render_candidates([]) == ""


def test_probe_696_render_candidates_includes_fields() -> None:
    """The rendered candidates block must include id, name, class,
    description fields. A scientist reviewing the prompt must see
    enough to verify the LLM saw the right components."""
    from apecx_integration.composition.composer import _render_candidates
    from apecx_integration.composition.component_catalog import (
        CatalogComponent, SearchHit,
    )
    hit = SearchHit(
        component=CatalogComponent(
            id="wf/x:1", name="x", description="some test desc",
            class_path="test.X", yaml_path="x.yml",
            examples=("ex1", "ex2"),
        ),
        score=2,
    )
    rendered = _render_candidates([hit])
    assert "wf/x:1" in rendered
    assert "test.X" in rendered
    assert "some test desc" in rendered


def test_probe_697_build_user_prompt_emits_no_match_hint(tmp_path) -> None:
    """When hits is empty, the user prompt must steer the LLM to
    emit novel Python via a "no matching library components" hint —
    a silent omission would let the LLM hallucinate library refs."""
    from apecx_integration.composition.composer import (
        Composer, _apply_llm_env_overrides,
    )
    p = _make_minimal_config_yaml(tmp_path)
    composer = Composer.from_config(p)
    rendered = composer._build_user_prompt("test task", hits=[], context=None)
    assert "no matching library components" in rendered
    assert "test task" in rendered


def test_probe_698_build_user_prompt_strips_internal_context(tmp_path) -> None:
    """run_id is internal plumbing; it must NOT appear in the
    LLM-visible prompt. Leaking it would (a) waste tokens and (b)
    risk the LLM using it in YAML output."""
    from apecx_integration.composition.composer import Composer
    from uuid import uuid4
    p = _make_minimal_config_yaml(tmp_path)
    composer = Composer.from_config(p)
    rid = str(uuid4())
    rendered = composer._build_user_prompt(
        "task", hits=[], context={"run_id": rid, "scientist_note": "careful here"},
    )
    assert rid not in rendered
    assert "scientist_note" in rendered  # public context still emitted


def test_probe_699_build_user_prompt_emits_additional_context_block(tmp_path) -> None:
    """Non-internal context keys must appear under an
    "Additional context" header so the LLM consumes them
    deterministically."""
    from apecx_integration.composition.composer import Composer
    p = _make_minimal_config_yaml(tmp_path)
    composer = Composer.from_config(p)
    rendered = composer._build_user_prompt(
        "task", hits=[], context={"hint": "use snapshot mode"},
    )
    assert "## Additional context" in rendered
    assert "hint:" in rendered


# ---------------------------------------------------------------------------
# Internal constants + compose() input validation — probes 700-704
# ---------------------------------------------------------------------------


def test_probe_700_internal_context_keys_locked() -> None:
    """run_id is the only internal-plumbing key today. Adding more
    requires conscious thought about what should be redacted from
    LLM prompts — lock the set."""
    from apecx_integration.composition.composer import _INTERNAL_CONTEXT_KEYS
    assert _INTERNAL_CONTEXT_KEYS == frozenset({"run_id"})


def test_probe_701_compose_rejects_empty_prompt(tmp_path) -> None:
    """An empty prompt is a no-op — must reject before calling the
    LLM (which would charge for a useless call)."""
    import asyncio
    from apecx_integration.composition.composer import Composer
    p = _make_minimal_config_yaml(tmp_path)
    composer = Composer.from_config(p)
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(composer.compose(""))


def test_probe_702_compose_rejects_non_string_prompt(tmp_path) -> None:
    import asyncio
    from apecx_integration.composition.composer import Composer
    p = _make_minimal_config_yaml(tmp_path)
    composer = Composer.from_config(p)
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(composer.compose(None))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(composer.compose(123))  # type: ignore[arg-type]


def test_probe_703_compose_rejects_whitespace_only(tmp_path) -> None:
    """A prompt of pure whitespace would also be a useless LLM
    call. Probe ensures it's rejected at the input gate."""
    import asyncio
    from apecx_integration.composition.composer import Composer
    p = _make_minimal_config_yaml(tmp_path)
    composer = Composer.from_config(p)
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(composer.compose("   \n\t  "))


def test_probe_704_error_classes_extend_value_error() -> None:
    """ComposerConfigurationError + ComposerResponseError must both
    extend ValueError. Callers that catch ``ValueError`` for ANY
    composer-related problem then get both for free."""
    from apecx_integration.composition.composer import (
        ComposerConfigurationError, ComposerResponseError,
    )
    assert issubclass(ComposerConfigurationError, ValueError)
    assert issubclass(ComposerResponseError, ValueError)
