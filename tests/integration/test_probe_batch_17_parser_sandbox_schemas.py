"""Probe batch 17 — composer parser, AST sandbox, composer schemas
+ artifact-store invariants (probes 430-454).

Targets pure-Python paths whose silent-failure modes would let
malformed LLM output, unsafe novel Python, or mis-shaped configs
slip past review:

  - composer._parse_response (LLM text → yaml + novel_python)
  - composition/sandbox.py (AST import scanner, BANNED_CALLS)
  - composer_schemas.ComposerConfig (Pydantic validation)
  - artifact_store invariants (kind / metadata pairing)

All probes are pure-Python — no DB, no FastAPI client, no LLM call.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Composer parser — probes 430-439
# ---------------------------------------------------------------------------


def test_probe_430_parser_extracts_yaml_block() -> None:
    from apecx_integration.composition.composer import _parse_response
    content = "Some prose.\n\n```yaml\nname: wf\nsteps: {}\n```\n"
    yaml_text, novel = _parse_response(content)
    assert "name: wf" in yaml_text
    assert novel == {}


def test_probe_431_parser_missing_yaml_raises() -> None:
    """No ```yaml fence at all → ComposerResponseError. Silent
    fallback to {} would let a malformed LLM response produce a
    blank workflow that "compiles" but does nothing."""
    from apecx_integration.composition.composer import (
        _parse_response, ComposerResponseError,
    )
    with pytest.raises(ComposerResponseError, match="yaml"):
        _parse_response("Just prose, no fences here.")


def test_probe_432_parser_multiple_yaml_first_wins() -> None:
    """LLMs sometimes emit a "preview" yaml followed by the real
    one. Spec: first-wins, log the rest. Probe: confirms second
    yaml block is NOT silently substituted."""
    from apecx_integration.composition.composer import _parse_response
    content = (
        "```yaml\nname: first\n```\n"
        "Some commentary.\n"
        "```yaml\nname: second\n```\n"
    )
    yaml_text, _ = _parse_response(content)
    assert "first" in yaml_text
    assert "second" not in yaml_text


def test_probe_433_parser_extracts_novel_python_dict() -> None:
    from apecx_integration.composition.composer import _parse_response
    content = (
        "```yaml\nname: wf\n```\n"
        "```novel_python\n"
        "rogue_step: |\n"
        "  def run(x):\n"
        "      return x\n"
        "```\n"
    )
    _, novel = _parse_response(content)
    assert "rogue_step" in novel
    assert "def run" in novel["rogue_step"]


def test_probe_434_parser_no_novel_python_means_empty() -> None:
    """Absence of a novel_python fence must yield {}, not crash."""
    from apecx_integration.composition.composer import _parse_response
    content = "```yaml\nname: wf\n```\n"
    _, novel = _parse_response(content)
    assert novel == {}


def test_probe_435_parser_novel_python_non_string_value_raises() -> None:
    """A non-string source (e.g. an int) under a step_id key must
    fail-fast — silently coercing would feed garbage to the AST
    scanner downstream."""
    from apecx_integration.composition.composer import (
        _parse_response, ComposerResponseError,
    )
    content = (
        "```yaml\nname: wf\n```\n"
        "```novel_python\n"
        "rogue_step: 42\n"
        "```\n"
    )
    with pytest.raises(ComposerResponseError, match="source string"):
        _parse_response(content)


def test_probe_436_parser_novel_python_non_mapping_raises() -> None:
    """A list (instead of mapping) under novel_python → reject."""
    from apecx_integration.composition.composer import (
        _parse_response, ComposerResponseError,
    )
    content = (
        "```yaml\nname: wf\n```\n"
        "```novel_python\n"
        "- step1\n- step2\n"
        "```\n"
    )
    with pytest.raises(ComposerResponseError, match="mapping"):
        _parse_response(content)


def test_probe_437_parser_novel_python_invalid_yaml_raises() -> None:
    from apecx_integration.composition.composer import (
        _parse_response, ComposerResponseError,
    )
    content = (
        "```yaml\nname: wf\n```\n"
        "```novel_python\n"
        "key: [unclosed\n"
        "```\n"
    )
    with pytest.raises(ComposerResponseError, match="novel_python"):
        _parse_response(content)


def test_probe_438_parser_ignores_prose_outside_fences() -> None:
    """Prose between fences must not be mistaken for fence content."""
    from apecx_integration.composition.composer import _parse_response
    content = (
        "Here is what I composed for you, scientist:\n\n"
        "```yaml\nname: composed\n```\n\n"
        "Hope that helps!"
    )
    yaml_text, _ = _parse_response(content)
    assert yaml_text.strip() == "name: composed"


def test_probe_439_parser_empty_novel_python_block_is_empty_dict() -> None:
    """An empty (or null) novel_python fence must yield {} —
    yaml.safe_load("") returns None, the parser must handle it."""
    from apecx_integration.composition.composer import _parse_response
    content = (
        "```yaml\nname: wf\n```\n"
        "```novel_python\n"
        "\n"
        "```\n"
    )
    _, novel = _parse_response(content)
    assert novel == {}


# ---------------------------------------------------------------------------
# Sandbox AST scanner — probes 440-445
# ---------------------------------------------------------------------------


def test_probe_440_sandbox_scans_imports() -> None:
    from apecx_integration.composition.sandbox import scan_python_source
    src = "import math\nimport os.path\n"
    result = scan_python_source(src)
    modules = {imp.module for imp in result.imports}
    assert {"math", "os"} <= modules


def test_probe_441_sandbox_bans_eval_exec_compile() -> None:
    """eval/exec/compile are dynamic-code constructs — banning them
    is a load-bearing isolation control. Each must be detected."""
    from apecx_integration.composition.sandbox import scan_python_source
    for banned in ("eval", "exec", "compile"):
        src = f"x = {banned}('1 + 1')\n"
        result = scan_python_source(src)
        assert not result.ok
        assert any(banned in v for v in result.violations), (
            f"PROBE 441: {banned} not flagged: {result.violations}"
        )


def test_probe_442_sandbox_bans_importlib_dynamic_import() -> None:
    """importlib.import_module bypasses static import scanning. Must
    flag — otherwise novel code can import arbitrary modules at
    runtime, defeating the whitelist."""
    from apecx_integration.composition.sandbox import scan_python_source
    src = "import importlib\nm = importlib.import_module('os')\n"
    result = scan_python_source(src)
    assert not result.ok
    assert any("import_module" in v for v in result.violations)


def test_probe_443_sandbox_rejects_relative_imports() -> None:
    from apecx_integration.composition.sandbox import scan_python_source
    src = "from .sibling import thing\n"
    result = scan_python_source(src)
    assert not result.ok
    assert any("relative" in v for v in result.violations)


def test_probe_444_sandbox_whitelist_enforcement() -> None:
    """An import not in the whitelist must violate. Empty whitelist
    means every top-level package is rejected — strictest possible
    setting."""
    from apecx_integration.composition.sandbox import scan_python_source
    src = "import pandas\n"
    # Whitelist has only 'numpy'; 'pandas' must be flagged
    result = scan_python_source(src, whitelist=frozenset({"numpy"}))
    assert not result.ok
    assert any("pandas" in v for v in result.violations)
    # With pandas whitelisted, must pass
    result_ok = scan_python_source(src, whitelist=frozenset({"pandas"}))
    assert result_ok.ok


def test_probe_445_sandbox_whitelist_loader_rejects_dotted(tmp_path) -> None:
    """A whitelist with dotted entries (e.g. 'pandas.DataFrame') is
    a misuse — entries must be top-level package names. Must
    fail-fast at load."""
    from apecx_integration.composition.sandbox import load_whitelist
    p = tmp_path / "wl.txt"
    p.write_text("# header\nnumpy\npandas.DataFrame\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level"):
        load_whitelist(p)


# ---------------------------------------------------------------------------
# Composer schemas — probes 446-449
# ---------------------------------------------------------------------------


def test_probe_446_composer_config_requires_library_version(tmp_path) -> None:
    """library_version is the AC3 anchor for every GeneratedArtifact
    row. ComposerConfig must reject configs without it."""
    from pydantic import ValidationError
    from apecx_integration.composition.composer_schemas import ComposerConfig
    with pytest.raises(ValidationError):
        ComposerConfig(prompt_dir=tmp_path)  # missing library_version


def test_probe_447_composer_config_max_tokens_must_be_positive(tmp_path) -> None:
    from pydantic import ValidationError
    from apecx_integration.composition.composer_schemas import ComposerConfig
    with pytest.raises(ValidationError):
        ComposerConfig(
            library_version="0.1.0",
            prompt_dir=tmp_path,
            max_tokens=0,
        )


def test_probe_448_composer_config_temperature_bounds(tmp_path) -> None:
    """temperature ∈ [0.0, 2.0]. Above 2 is non-deterministic noise;
    below 0 is undefined."""
    from pydantic import ValidationError
    from apecx_integration.composition.composer_schemas import ComposerConfig
    # Valid
    cfg = ComposerConfig(
        library_version="0.1.0", prompt_dir=tmp_path, temperature=0.0
    )
    assert cfg.temperature == 0.0
    cfg2 = ComposerConfig(
        library_version="0.1.0", prompt_dir=tmp_path, temperature=2.0
    )
    assert cfg2.temperature == 2.0
    # Invalid
    with pytest.raises(ValidationError):
        ComposerConfig(
            library_version="0.1.0", prompt_dir=tmp_path, temperature=-0.1
        )
    with pytest.raises(ValidationError):
        ComposerConfig(
            library_version="0.1.0", prompt_dir=tmp_path, temperature=2.1
        )


def test_probe_449_composer_config_max_retries_non_negative(tmp_path) -> None:
    """max_retries=-1 is meaningless. Pydantic ge=0 must enforce."""
    from pydantic import ValidationError
    from apecx_integration.composition.composer_schemas import ComposerConfig
    with pytest.raises(ValidationError):
        ComposerConfig(
            library_version="0.1.0", prompt_dir=tmp_path, max_retries=-1
        )


# ---------------------------------------------------------------------------
# Artifact store invariants — probes 450-454
# ---------------------------------------------------------------------------


def test_probe_450_generated_kinds_require_metadata() -> None:
    """ArtifactStore.store rejects GENERATED_* kind without
    GenerationMetadata — the metadata is what makes it a generated
    artifact. The check must happen BEFORE any disk write."""
    import inspect
    from apecx_integration.composition.artifact_store import ArtifactStore
    src = inspect.getsource(ArtifactStore.store)
    # Both halves of the gate must be in the source. We can't run
    # store() without a real DB, but we can verify the gates exist.
    assert "GENERATED_KINDS" in src
    assert "generated_metadata is None" in src
    assert "generated_metadata is not None" in src


def test_probe_451_artifact_not_found_is_lookup_error() -> None:
    """ArtifactNotFound must extend LookupError so generic
    except-blocks can catch it without importing the module."""
    from apecx_integration.composition.artifact_store import ArtifactNotFound
    assert issubclass(ArtifactNotFound, LookupError)


def test_probe_452_generated_kinds_set_locked() -> None:
    """GENERATED_KINDS must contain exactly GENERATED_WORKFLOW and
    GENERATED_PYTHON. A future kind being silently added (or one
    being removed) would break the metadata-required invariant."""
    from apecx_integration.composition.artifact_store import GENERATED_KINDS
    from apecx_integration.control_plane.schemas.enums import ArtifactKind
    assert GENERATED_KINDS == frozenset({
        ArtifactKind.GENERATED_WORKFLOW,
        ArtifactKind.GENERATED_PYTHON,
    })


def test_probe_453_generation_metadata_required_fields() -> None:
    """GenerationMetadata is a frozen dataclass with required fields.
    Missing any required field must raise TypeError at construct
    time (frozen + kw_only)."""
    from apecx_integration.composition.artifact_store import GenerationMetadata
    # All required fields → ok
    md = GenerationMetadata(
        source_prompt="prompt",
        library_version="0.1.0",
        llm_model="m",
        llm_model_version_hash="h",
    )
    assert md.composition_summary == {}
    assert md.parent_artifact_id is None
    # Missing source_prompt → TypeError
    with pytest.raises(TypeError):
        GenerationMetadata(  # type: ignore[call-arg]
            library_version="0.1.0",
            llm_model="m",
            llm_model_version_hash="h",
        )


def test_probe_454_generation_metadata_is_frozen() -> None:
    """Frozen dataclass — composition_summary mutations after
    construction would silently drift the audit trail. Probe
    confirms the dataclass-level frozen guarantee."""
    from dataclasses import FrozenInstanceError
    from apecx_integration.composition.artifact_store import GenerationMetadata
    md = GenerationMetadata(
        source_prompt="p",
        library_version="0.1.0",
        llm_model="m",
        llm_model_version_hash="h",
    )
    with pytest.raises(FrozenInstanceError):
        md.llm_model = "different"  # type: ignore[misc]
