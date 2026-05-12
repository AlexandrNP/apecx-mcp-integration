"""Unit tests for ``PromptBudget`` + composer load-time cap enforcement.

The prompt-budget machinery (composer_schemas.PromptBudget +
Composer._enforce_prompt_budgets) is the framework-native enforcement
consumer for the "system.md size discipline" callout in the
supervisor handbook. It defends against the silent failure mode:
prompt drifts past the 12B model's instruction-following budget,
T01 AC1 still passes (1 sample!), but multi-step rule-following
degrades.

Pins:
  1. PromptBudget structural behavior (size, fraction, cap predicates).
  2. Composer's prompt_budgets property returns one entry per loaded
     prompt (system, composition_bias, novel_python_flagging,
     spec_system — confirmed against current composer_config.yml).
  3. The CURRENT system.md is within the SOFT cap (regression catch
     — a future rule addition pushing past 14 KB fails this test
     before it lands in main).
  4. Composer raises ComposerConfigurationError when a config sets
     prompt_hard_cap_kb below the actual system.md size.
  5. Composer warns (does NOT raise) when prompt_soft_cap_kb is
     below the actual system.md size but the hard cap is not breached.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from apecx_integration.composition._errors import ComposerConfigurationError
from apecx_integration.composition.composer import Composer
from apecx_integration.composition.composer_schemas import PromptBudget

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CFG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"


def _budget(*, size: int, soft: int = 14 * 1024, hard: int = 16 * 1024) -> PromptBudget:
    return PromptBudget(
        name="test",
        size_bytes=size,
        soft_cap_bytes=soft,
        hard_cap_bytes=hard,
    )


# ---------------------------------------------------------------------------
# 1. Structural behavior
# ---------------------------------------------------------------------------


def test_fraction_of_hard_cap_basic() -> None:
    b = _budget(size=8192)  # 50% of 16 KB hard cap
    assert b.fraction_of_hard_cap == 0.5


def test_fraction_at_boundaries() -> None:
    assert _budget(size=0).fraction_of_hard_cap == 0.0
    assert _budget(size=16 * 1024).fraction_of_hard_cap == 1.0
    # Over-cap reports > 1.0 (object exists for diagnostic introspection
    # even when the composer raised at load).
    assert _budget(size=32 * 1024).fraction_of_hard_cap == 2.0


def test_is_within_soft_cap() -> None:
    assert _budget(size=10 * 1024).is_within_soft_cap is True
    assert _budget(size=14 * 1024).is_within_soft_cap is True  # boundary
    assert _budget(size=14 * 1024 + 1).is_within_soft_cap is False


def test_is_within_hard_cap() -> None:
    assert _budget(size=15 * 1024).is_within_hard_cap is True
    assert _budget(size=16 * 1024).is_within_hard_cap is True  # boundary
    assert _budget(size=16 * 1024 + 1).is_within_hard_cap is False


def test_budget_is_frozen() -> None:
    import dataclasses

    b = _budget(size=1024)
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.size_bytes = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Composer wiring + regression catch
# ---------------------------------------------------------------------------


def test_composer_exposes_prompt_budgets() -> None:
    c = Composer.from_config(str(COMPOSER_CFG))
    budgets = c.prompt_budgets
    assert "system" in budgets, "system prompt budget must be present"
    # composition_bias is in REQUIRED_PROMPT_FILES so it must load.
    assert "composition_bias" in budgets


def test_prompt_budgets_returns_a_copy_not_internal_dict() -> None:
    """Mutating the returned dict must NOT affect the composer's
    internal snapshot — operators read the budgets as a frozen view."""
    c = Composer.from_config(str(COMPOSER_CFG))
    first = c.prompt_budgets
    first["fake"] = _budget(size=1)  # type: ignore[assignment]
    second = c.prompt_budgets
    assert "fake" not in second


def test_current_system_md_is_within_soft_cap_regression() -> None:
    """REGRESSION CATCH — a future rule addition that pushes
    system.md past 14 KB fails this test before it lands in main.

    If this test breaks legitimately (e.g., a deliberate consolidation
    pass that lifts the cap), update ``prompt_soft_cap_kb`` in
    ``composer_config.yml`` and document the reason in the commit
    body. Do NOT silently skip this test."""
    c = Composer.from_config(str(COMPOSER_CFG))
    system = c.prompt_budgets["system"]
    assert system.is_within_soft_cap, (
        f"system.md is {system.size_bytes} bytes "
        f"({system.size_bytes / 1024:.2f} KB), exceeding the soft "
        f"cap of {system.soft_cap_bytes / 1024:.2f} KB. Either trim "
        f"the prompt OR lift prompt_soft_cap_kb in composer_config.yml "
        f"with a justified commit body."
    )


# ---------------------------------------------------------------------------
# 3. Hard cap raise + soft cap warn at composer load
# ---------------------------------------------------------------------------


def _stage_config(tmp_path: Path, *, soft_kb: float, hard_kb: float) -> Path:
    """Stage a composer_config.yml in tmp_path that points at the
    real prompt_dir but overrides the cap thresholds."""
    real_cfg = yaml.safe_load(COMPOSER_CFG.read_text(encoding="utf-8"))
    # The real config uses relative paths resolved against the config
    # file's directory. We rewrite them to absolute paths so the
    # composer can find the files from tmp_path.
    cfg_dir = COMPOSER_CFG.parent
    real_cfg["prompt_dir"] = str((cfg_dir / real_cfg["prompt_dir"]).resolve())
    real_cfg["component_catalog_paths"] = [
        str((cfg_dir / p).resolve()) for p in real_cfg.get("component_catalog_paths", [])
    ]
    if real_cfg.get("sandbox_whitelist_path"):
        real_cfg["sandbox_whitelist_path"] = str(
            (cfg_dir / real_cfg["sandbox_whitelist_path"]).resolve()
        )
    if real_cfg.get("rag_index_dir"):
        real_cfg["rag_index_dir"] = str((cfg_dir / real_cfg["rag_index_dir"]).resolve())
    real_cfg["prompt_soft_cap_kb"] = soft_kb
    real_cfg["prompt_hard_cap_kb"] = hard_kb
    out = tmp_path / "composer_config.yml"
    out.write_text(yaml.safe_dump(real_cfg), encoding="utf-8")
    return out


def test_hard_cap_breach_raises_at_composer_load(tmp_path: Path) -> None:
    """Set hard cap deliberately below current system.md size; the
    composer must FAIL-FAST per nanobrain discipline."""
    cfg_path = _stage_config(tmp_path, soft_kb=1.0, hard_kb=1.0)
    with pytest.raises(ComposerConfigurationError, match=r"hard cap"):
        Composer.from_config(str(cfg_path))


def test_soft_cap_breach_warns_but_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Soft cap below current size but hard cap above: composer
    starts, but logs a warning. Silent acceptance is the failure
    mode we're guarding against — the warning is the surface."""
    # 1 KB soft is below current ~12.5 KB; 32 KB hard is well above.
    cfg_path = _stage_config(tmp_path, soft_kb=1.0, hard_kb=32.0)
    with caplog.at_level(logging.WARNING, logger="apecx_integration.composition.composer"):
        c = Composer.from_config(str(cfg_path))
    assert c is not None
    # The warning record must mention the breach context.
    soft_breach_records = [r for r in caplog.records if "soft cap" in r.getMessage().lower()]
    assert soft_breach_records, (
        "expected a WARNING log mentioning the soft cap breach; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )


def test_non_system_prompts_do_not_trigger_hard_cap_raise(tmp_path: Path) -> None:
    """Only ``system.md`` is hard-capped. Other prompts can be
    arbitrarily large without blocking composer load — they get a
    soft-cap INFO log instead (deliberate carve-out: an operator
    may legitimately ship a long sub-prompt for a specialized
    deployment without it taking the composer down)."""
    # Pick a hard cap that's between composition_bias (small) and
    # system.md size. Configure cap WAY above to avoid system raise,
    # then trust that no other prompt is bigger than system. The pin
    # here is "Composer starts when only sub-prompts are over soft."
    cfg_path = _stage_config(tmp_path, soft_kb=0.5, hard_kb=64.0)
    # composition_bias is ~3 KB > 0.5 KB soft, so it triggers the
    # INFO log; system.md is well within 64 KB hard. Composer must
    # construct.
    c = Composer.from_config(str(cfg_path))
    assert "composition_bias" in c.prompt_budgets
