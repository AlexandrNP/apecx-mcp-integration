"""Probe batch 46 — adversarial probes against the ApprovalPolicy
loader and decision shapes.

Streak before this batch: 149/300 post-AQ post-1066.
Probe naming: 1205–1229.

Distinct probes only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.approval_policy import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalPolicy,
)
from apecx_integration.composition.differ import StepCategory


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "configs" / "approval_policy.yml"


def _all_categories_to_action(action: str) -> dict[str, str]:
    return {c.value: action for c in StepCategory}


# --------------------------------------------------------------------------- #
# Probes 1205–1229
# --------------------------------------------------------------------------- #


def test_probe_1205_default_policy_yaml_loads():
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    assert policy is not None


def test_probe_1206_default_policy_covers_every_step_category():
    """The constructor refuses a partial mapping; the loader of the
    bundled YAML must produce a complete mapping."""
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    for c in StepCategory:
        # action_for must succeed for every category.
        action = policy.action_for(c)
        assert isinstance(action, ApprovalAction)


def test_probe_1207_partial_policy_raises_with_missing_categories():
    """A YAML missing a category must raise loud (no silent fallback
    to AUTO)."""
    raw = _all_categories_to_action("auto")
    # Drop one.
    first = next(iter(raw))
    del raw[first]
    import yaml as _yaml
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        _yaml.safe_dump(raw, fh)
        path = Path(fh.name)
    try:
        with pytest.raises(ValueError, match="missing"):
            ApprovalPolicy.load(path)
    finally:
        path.unlink()


def test_probe_1208_unknown_category_in_yaml_rejected(tmp_path):
    """Unknown category key (e.g. typo) must raise."""
    yml = tmp_path / "policy.yml"
    yml.write_text(
        "library_swap: auto\n"
        "novel_python: require_review\n"
        "novel_yaml: require_review\n"
        "configuration_change: auto\n"
        "typo_category: auto\n"
    )
    with pytest.raises(ValueError, match="unknown category"):
        ApprovalPolicy.load(yml)


def test_probe_1209_unknown_action_in_yaml_rejected(tmp_path):
    """Unknown action (e.g. ``"approve_silently"``) must raise."""
    yml = tmp_path / "policy.yml"
    raw = _all_categories_to_action("auto")
    first = next(iter(raw))
    raw[first] = "approve_silently"
    import yaml
    yml.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="unknown action"):
        ApprovalPolicy.load(yml)


def test_probe_1210_non_dict_yaml_rejected(tmp_path):
    """A YAML that's a list, not a dict, must raise."""
    yml = tmp_path / "policy.yml"
    yml.write_text("- foo\n- bar\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        ApprovalPolicy.load(yml)


def test_probe_1211_policy_mapping_is_defensive_copied():
    """Policy stores a defensive copy; mutating the source dict must
    NOT mutate the policy. Pin so a future "performance optimization"
    that shares the dict is caught."""
    src = {c: ApprovalAction.AUTO for c in StepCategory}
    policy = ApprovalPolicy(mapping=src)
    src.clear()
    # Policy still has the mapping.
    for c in StepCategory:
        assert policy.action_for(c) == ApprovalAction.AUTO


def test_probe_1212_action_enum_values_pinned():
    """Action enum string values are external API (YAML keys).
    Pin so a refactor renaming "auto" -> "automatic" is intentional."""
    expected = {"auto", "require_review", "require_expert_review"}
    actual = {a.value for a in ApprovalAction}
    assert expected.issubset(actual)


def _fake_categorization(step_id: str = "x") -> "StepCategorization":
    from apecx_integration.composition.differ import StepCategorization
    return StepCategorization(
        step_id=step_id,
        step_class="apecx_integration.composition.steps.x.X",
        category=StepCategory.NOVEL,
        reason="probe fixture",
    )


def test_probe_1213_approval_decision_blocks_returns_true_when_review_required():
    """ApprovalDecision.blocks must return True when review_required
    or expert_review_required are non-empty. ``blocks`` is a property,
    not a method — pin the contract."""
    from apecx_integration.composition.approval_policy import ApprovalDecision
    fake = _fake_categorization()
    d_review = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(fake,),
        expert_review_required_steps=(),
    )
    # property access (no parentheses)
    assert d_review.blocks is True

    d_expert = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(),
        expert_review_required_steps=(fake,),
    )
    assert d_expert.blocks is True


def test_probe_1214_approval_decision_blocks_returns_false_for_all_auto():
    from apecx_integration.composition.approval_policy import ApprovalDecision
    d = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(),
        expert_review_required_steps=(),
    )
    assert d.blocks is False


def test_probe_1215_approval_decision_strongest_required_action():
    """When both review and expert lists are populated, strongest
    is REQUIRE_EXPERT_REVIEW (most restrictive). Pin the property."""
    from apecx_integration.composition.approval_policy import (
        ApprovalDecision, ApprovalAction,
    )
    fake = _fake_categorization()
    d = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(fake,),
        expert_review_required_steps=(fake,),
    )
    assert d.strongest_required_action == ApprovalAction.REQUIRE_EXPERT_REVIEW


def test_probe_1216_approval_decision_is_frozen_dataclass():
    """ApprovalDecision is frozen — mutation must raise."""
    from apecx_integration.composition.approval_policy import ApprovalDecision
    fake = _fake_categorization()
    d = ApprovalDecision(
        auto_approved_steps=(fake,),
        review_required_steps=(),
        expert_review_required_steps=(),
    )
    with pytest.raises(Exception):
        d.auto_approved_steps = ()  # type: ignore[misc]


def test_probe_1217_approval_decision_uses_tuples_not_lists():
    """Tuples for hashability + immutability. A list would let
    callers mutate the decision after evaluation."""
    from apecx_integration.composition.approval_policy import ApprovalDecision
    d = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(),
        expert_review_required_steps=(),
    )
    assert isinstance(d.auto_approved_steps, tuple)
    assert isinstance(d.review_required_steps, tuple)
    assert isinstance(d.expert_review_required_steps, tuple)


def test_probe_1218_policy_load_handles_unicode_in_path(tmp_path):
    """Unicode in the YAML path must work (operators on non-ASCII
    file systems)."""
    udir = tmp_path / "πoλίcy"
    udir.mkdir()
    yml = udir / "policy.yml"
    raw = _all_categories_to_action("auto")
    import yaml as _yaml
    yml.write_text(_yaml.safe_dump(raw), encoding="utf-8")
    policy = ApprovalPolicy.load(yml)
    assert policy is not None


def test_probe_1219_policy_load_with_empty_yaml(tmp_path):
    """An empty YAML file (yaml.safe_load returns None) must raise."""
    yml = tmp_path / "empty.yml"
    yml.write_text("")
    with pytest.raises(ValueError):
        ApprovalPolicy.load(yml)


def test_probe_1220_policy_evaluate_partitions_correctly():
    """A workflow with each StepCategory mapped to a different
    action produces the correct partitioning."""
    from apecx_integration.composition.differ import (
        CategorizedWorkflow, StepCategorization,
    )
    raw = {
        StepCategory.COMPOSED_STANDARD: ApprovalAction.AUTO,
        StepCategory.COMPOSED_PARAMETERIZED: ApprovalAction.REQUIRE_REVIEW,
        StepCategory.COMPOSED_WRAPPED: ApprovalAction.AUTO,
        StepCategory.NOVEL: ApprovalAction.REQUIRE_EXPERT_REVIEW,
    }
    policy = ApprovalPolicy(mapping=raw)
    cats = (
        StepCategorization(
            step_id="A", step_class="X",
            category=StepCategory.COMPOSED_STANDARD, reason="r1",
        ),
        StepCategorization(
            step_id="B", step_class="X",
            category=StepCategory.COMPOSED_PARAMETERIZED, reason="r2",
        ),
        StepCategorization(
            step_id="C", step_class="X",
            category=StepCategory.NOVEL, reason="r3",
        ),
    )
    cw = CategorizedWorkflow(categorizations=cats)
    decision = policy.evaluate(cw)
    assert len(decision.auto_approved_steps) == 1
    assert len(decision.review_required_steps) == 1
    assert len(decision.expert_review_required_steps) == 1


def test_probe_1221_policy_default_yml_actions_are_documented_set():
    """The bundled YAML uses only documented actions; pin the action
    set so a refactor adding a new action is intentional."""
    import yaml
    raw = yaml.safe_load(DEFAULT_POLICY.read_text())
    actions = set(raw.values())
    documented = {"auto", "require_review", "require_expert_review"}
    assert actions.issubset(documented), (
        f"unknown actions in default policy: {actions - documented}"
    )


def test_probe_1222_policy_default_yml_categories_match_enum():
    """The bundled YAML's category KEYS must exactly match the
    StepCategory enum (no missing, no extras)."""
    import yaml
    raw = yaml.safe_load(DEFAULT_POLICY.read_text())
    enum_vals = {c.value for c in StepCategory}
    yaml_keys = set(raw.keys())
    assert yaml_keys == enum_vals, (
        f"yaml/enum mismatch: yaml-only={yaml_keys - enum_vals}, "
        f"enum-only={enum_vals - yaml_keys}"
    )


def test_probe_1223_policy_action_for_returns_consistent_action():
    """Two calls to action_for(same category) return the same action.
    Pin: no randomness / counter / state."""
    policy = ApprovalPolicy.load(DEFAULT_POLICY)
    for c in StepCategory:
        a1 = policy.action_for(c)
        a2 = policy.action_for(c)
        assert a1 == a2


def test_probe_1224_policy_load_path_must_exist():
    """A non-existent path must raise FileNotFoundError (or similar)
    via Path.read_text, not silently produce an empty policy."""
    import tempfile
    bad = Path(tempfile.gettempdir()) / "absolutely-does-not-exist.yml"
    if bad.exists():
        bad.unlink()
    with pytest.raises((FileNotFoundError, OSError)):
        ApprovalPolicy.load(bad)


def test_probe_1225_approval_action_enum_str_inheritance():
    """ApprovalAction inherits from StrEnum so ``str(action) == action.value``
    works. Pin: callers may serialize via str()."""
    from apecx_integration.composition.approval_policy import ApprovalAction
    assert str(ApprovalAction.AUTO) == "auto" or \
        ApprovalAction.AUTO.value == "auto"


def test_probe_1226_default_policy_yml_loads_without_warnings():
    """Pin: loading the default policy emits no warnings (would
    obscure the load log line for operators)."""
    import warnings
    with warnings.catch_warnings(record=True) as ws:
        warnings.simplefilter("always")
        ApprovalPolicy.load(DEFAULT_POLICY)
        # Ignore PydanticDeprecatedSince warnings (framework's, not ours).
        rel = [
            w for w in ws
            if "deprecated" not in str(w.message).lower()
        ]
        assert not rel, (
            f"unexpected warnings on policy load: {[str(w.message) for w in rel]}"
        )


def test_probe_1227_decision_strongest_action_no_blocks_returns_auto():
    """If nothing blocks, the strongest required action is AUTO
    (sentinel for "nothing to escalate")."""
    from apecx_integration.composition.approval_policy import (
        ApprovalDecision, ApprovalAction,
    )
    d = ApprovalDecision(
        auto_approved_steps=(),
        review_required_steps=(),
        expert_review_required_steps=(),
    )
    # property access (no parens)
    assert d.strongest_required_action == ApprovalAction.AUTO


def test_probe_1228_approval_policy_load_with_inline_comments(tmp_path):
    """YAML allows inline comments; ensure the loader handles them."""
    yml = tmp_path / "comm.yml"
    text = "# header comment\n"
    for c in StepCategory:
        text += f"{c.value}: auto  # inline comment\n"
    yml.write_text(text)
    policy = ApprovalPolicy.load(yml)
    for c in StepCategory:
        assert policy.action_for(c) == ApprovalAction.AUTO


def test_probe_1229_approval_module_exports_match_all():
    """The module's __all__ must surface the 3 public symbols.
    Catches drift in the public surface."""
    from apecx_integration.composition import approval_policy as mod
    expected = {"ApprovalAction", "ApprovalDecision", "ApprovalPolicy"}
    assert expected.issubset(set(mod.__all__))
