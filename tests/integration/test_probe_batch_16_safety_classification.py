"""Probe batch 16 — safety + classification surface (probes 405-429).

Targets four pure-Python modules whose bugs would manifest as
"silent failure that lets bad workflows through review":

  - Docker sandbox argv construction (composition/docker_sandbox.py)
  - Transforms (composition/transforms.py)
  - Workflow differ / categorization (composition/differ.py)
  - Approval policy (composition/approval_policy.py)

Each probe is a distinct adversarial scenario. All probes are
pure-Python; no DB / no FastAPI / no Docker invocation.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Docker sandbox argv — probes 405-414
# ---------------------------------------------------------------------------


def _argv() -> list[str]:
    from apecx_integration.composition.docker_sandbox import (
        build_docker_sandbox_command,
    )

    return build_docker_sandbox_command(["python", "x.py"], input_host_path=None)


def test_probe_405_docker_argv_starts_with_run() -> None:
    argv = _argv()
    assert argv[0] == "docker"
    assert argv[1] == "run"
    assert "--rm" in argv


def test_probe_406_docker_argv_network_none() -> None:
    """The threat-model table mandates --network=none. Weakening
    this to bridge would let a sandboxed novel-Python step exfiltrate
    data."""
    argv = _argv()
    assert "--network" in argv
    i = argv.index("--network")
    assert argv[i + 1] == "none"


def test_probe_407_docker_argv_read_only_root() -> None:
    """--read-only mandated; without it, a novel script can write
    arbitrary content to the container's root filesystem and
    persist it across runs (escape via volume mount edge cases)."""
    assert "--read-only" in _argv()


def test_probe_408_docker_argv_cap_drop_all() -> None:
    argv = _argv()
    assert "--cap-drop" in argv
    i = argv.index("--cap-drop")
    assert argv[i + 1] == "ALL"


def test_probe_409_docker_argv_memory_swap_disabled() -> None:
    """--memory-swap MUST equal --memory or swap is unenforced.
    Swappable memory lets a runaway novel-Python step starve the
    host."""
    argv = _argv()
    i_mem = argv.index("--memory")
    i_swap = argv.index("--memory-swap")
    assert argv[i_mem + 1] == argv[i_swap + 1]


def test_probe_410_docker_argv_user_nonroot() -> None:
    """--user must NOT be root. 65534:65534 = nobody:nogroup."""
    argv = _argv()
    i = argv.index("--user")
    assert argv[i + 1] == "65534:65534"
    # Sanity: not 0, not 0:0, not root
    assert argv[i + 1] not in {"0", "0:0", "root", "root:root"}


def test_probe_411_docker_argv_pids_limit() -> None:
    """A pids-limit blocks fork-bomb DoS from inside the container."""
    argv = _argv()
    assert "--pids-limit" in argv


def test_probe_412_docker_argv_security_opts() -> None:
    """no-new-privileges blocks setuid escalation.

    2026-05-11: the original probe required BOTH
    ``no-new-privileges:true`` AND ``seccomp=default``. The latter was
    REMOVED from ``docker_sandbox.py`` (nanobrain CLAUDE.md
    "deployment-validation chain"):

      'Real bug surfaced + fixed in apecx-mcp-integration
      (--security-opt seccomp=default is not a Docker keyword;
      Docker Desktop on Mac treats it as a file path).'

    The container still runs with Docker's default seccomp profile
    implicitly — passing the flag explicitly is what broke on Mac.
    Probe relaxed to lock no-new-privileges only; seccomp coverage
    is integration-tested via ``test_docker_sandbox_runtime.py`` when
    ``APECX_T13B_SANDBOX_EXECUTE=1`` is set against a real daemon."""
    argv = _argv()
    sec_indices = [i for i, a in enumerate(argv) if a == "--security-opt"]
    assert len(sec_indices) >= 1, f"expected at least one --security-opt; argv={argv!r}"
    sec_values = {argv[i + 1] for i in sec_indices}
    assert "no-new-privileges:true" in sec_values
    # Defensive: if seccomp=default is ever re-introduced, the
    # probe pin documents that the Docker-Desktop-Mac path-treat
    # surprise is back. Allow it but don't require it.
    assert (
        "seccomp=default" not in sec_values
        or sec_values - {"no-new-privileges:true", "seccomp=default"} == set()
    ), (
        "seccomp=default was deliberately removed (Docker Desktop "
        "on Mac parses it as a file path). If you re-introduced it, "
        "verify the Mac path-resolution surprise is fixed upstream "
        "before merging."
    )


def test_probe_413_docker_argv_no_mount_when_no_input() -> None:
    """When input_host_path=None, NO --mount flag must appear.
    Adding a stray bind mount to the argv when the caller didn't
    ask for one would be a silent privilege expansion."""
    argv = _argv()
    assert "--mount" not in argv


def test_probe_414_docker_argv_command_after_image(tmp_path) -> None:
    """The command argv must be appended AFTER the image name, not
    before. Reversing them silently runs the image's entrypoint
    against arbitrary args, not the intended command."""
    from apecx_integration.composition.docker_sandbox import (
        SandboxConfig,
        build_docker_sandbox_command,
    )

    cfg = SandboxConfig(image="python:3.12-slim")
    argv = build_docker_sandbox_command(
        ["python", "-c", "print('hi')"],
        input_host_path=None,
        config=cfg,
    )
    image_idx = argv.index("python:3.12-slim")
    # Command must be at the end, after the image
    assert argv[image_idx + 1] == "python"
    assert argv[-1] == "print('hi')"


# ---------------------------------------------------------------------------
# Transforms — probes 415-419
# ---------------------------------------------------------------------------


def test_probe_415_entities_passthrough_query_terms() -> None:
    """If the upstream step already produced query_terms, the
    transform should pass them through unchanged — no double-flatten."""
    from apecx_integration.composition.transforms import (
        entities_to_query_terms,
    )

    out = entities_to_query_terms({"query_terms": ["a", "b", "c"]})
    assert out == {"query_terms": ["a", "b", "c"]}


def test_probe_416_entities_extract_names() -> None:
    from apecx_integration.composition.transforms import (
        entities_to_query_terms,
    )

    out = entities_to_query_terms(
        {
            "entities": [
                {"name": "EEEV", "type": "virus", "confidence": 0.95},
                {"name": "VEEV", "type": "virus", "confidence": 0.90},
            ]
        }
    )
    assert out == {"query_terms": ["EEEV", "VEEV"]}


def test_probe_417_entities_empty_input_safe() -> None:
    """Empty input must return {"query_terms": []}, not crash —
    the wrapping step enforces shape requirements; this transform
    just renames/flattens."""
    from apecx_integration.composition.transforms import (
        entities_to_query_terms,
    )

    assert entities_to_query_terms({}) == {"query_terms": []}
    # Entities present but malformed (missing 'name') → skipped
    assert entities_to_query_terms(
        {"entities": [{"type": "x"}, "not-a-dict", {"name": "kept"}]}
    ) == {"query_terms": ["kept"]}


def test_probe_418_proposals_rename_keys() -> None:
    from apecx_integration.composition.transforms import (
        llm_proposals_to_approved_mappings,
    )

    out = llm_proposals_to_approved_mappings(
        {
            "llm_proposals": [
                {"query_entity": "EEEV", "synonym": "Eastern Equine", "score": 0.9},
            ]
        }
    )
    assert out == {
        "approved_mappings": [
            {
                "query_term": "EEEV",
                "canonical_term": "Eastern Equine",
                "confidence": 0.9,
                "source_run_id": None,
                "comment": None,
            }
        ]
    }


def test_probe_419_proposals_preserve_reviewer_fields() -> None:
    """If reviewer metadata (source_run_id, comment) is present in
    the proposal, the transform must NOT silently drop it."""
    from apecx_integration.composition.transforms import (
        llm_proposals_to_approved_mappings,
    )

    out = llm_proposals_to_approved_mappings(
        {
            "llm_proposals": [
                {
                    "query_entity": "EEEV",
                    "synonym": "Eastern Equine",
                    "score": 0.9,
                    "source_run_id": "run-xyz",
                    "comment": "verified by reviewer",
                },
            ]
        }
    )
    assert out["approved_mappings"][0]["source_run_id"] == "run-xyz"
    assert out["approved_mappings"][0]["comment"] == "verified by reviewer"


# ---------------------------------------------------------------------------
# Differ / categorization — probes 420-424
# ---------------------------------------------------------------------------


def test_probe_420_differ_novel_python_step() -> None:
    """A step listed in novel_python is NOVEL regardless of its
    declared class. Treating it as composed would mean "review-
    required" gets bypassed for novel code."""
    from apecx_integration.composition.differ import (
        StepCategory,
        categorize_workflow,
    )

    wf = {"steps": {"s1": {"class": "library.SomeStep"}}}
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={"s1": "def run(): pass"},
        retrieved_class_paths={"library.SomeStep"},
    )
    assert result.categorizations[0].category is StepCategory.NOVEL


def test_probe_421_differ_orphan_class_is_novel() -> None:
    """A class not in the retrieval set falls to NOVEL — the
    correct failure mode. Treating an unknown class as composed
    would let mis-spelled or hallucinated paths bypass review."""
    from apecx_integration.composition.differ import (
        StepCategory,
        categorize_workflow,
    )

    wf = {"steps": {"s1": {"class": "made.Up.Class"}}}
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={},
        retrieved_class_paths={"library.RealStep"},
    )
    assert result.categorizations[0].category is StepCategory.NOVEL


def test_probe_422_differ_canonical_yaml_is_standard() -> None:
    from apecx_integration.composition.differ import (
        StepCategory,
        categorize_workflow,
    )

    wf = {"steps": {"s1": {"class": "library.X", "config": "library/x.yml"}}}
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={},
        retrieved_class_paths={"library.X"},
        catalog_yaml_paths={"library.X": "library/x.yml"},
    )
    assert result.categorizations[0].category is StepCategory.COMPOSED_STANDARD


def test_probe_423_differ_inline_config_is_parameterized() -> None:
    from apecx_integration.composition.differ import (
        StepCategory,
        categorize_workflow,
    )

    wf = {"steps": {"s1": {"class": "library.X", "config": {"k": 1}}}}
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={},
        retrieved_class_paths={"library.X"},
        catalog_yaml_paths={"library.X": "library/x.yml"},
    )
    assert result.categorizations[0].category is StepCategory.COMPOSED_PARAMETERIZED


def test_probe_424_differ_config_refs_novel_is_wrapped() -> None:
    """A library class whose config field references a novel step
    is COMPOSED_WRAPPED — needs review even though its top-level
    class is library."""
    from apecx_integration.composition.differ import (
        StepCategory,
        categorize_workflow,
    )

    wf = {
        "steps": {
            "wrapper": {
                "class": "library.X",
                "config": {"preprocessor": "rogue_extractor"},
            },
            "rogue_extractor": {"class": "novel.Code"},
        }
    }
    result = categorize_workflow(
        workflow_dict=wf,
        novel_python={"rogue_extractor": "def run(): pass"},
        retrieved_class_paths={"library.X"},
    )
    by_id = {c.step_id: c for c in result.categorizations}
    assert by_id["wrapper"].category is StepCategory.COMPOSED_WRAPPED
    assert by_id["rogue_extractor"].category is StepCategory.NOVEL


# ---------------------------------------------------------------------------
# Approval policy — probes 425-429
# ---------------------------------------------------------------------------


def test_probe_425_policy_rejects_incomplete_mapping() -> None:
    """A policy that doesn't map every category would silently
    KeyError later when an unmapped category appeared. Constructor
    must reject."""
    from apecx_integration.composition.approval_policy import (
        ApprovalAction,
        ApprovalPolicy,
    )
    from apecx_integration.composition.differ import StepCategory

    with pytest.raises(ValueError, match="missing"):
        ApprovalPolicy(
            mapping={
                StepCategory.NOVEL: ApprovalAction.REQUIRE_REVIEW,
            }
        )  # missing other categories


def test_probe_426_policy_load_rejects_unknown_category(tmp_path) -> None:
    """A typo in category name (e.g. 'composed_stnadard') must
    fail at load, not silently get ignored."""
    import yaml

    from apecx_integration.composition.approval_policy import ApprovalPolicy

    p = tmp_path / "policy.yml"
    p.write_text(
        yaml.safe_dump(
            {
                "composed_stnadard": "auto",  # typo
                "composed_standard": "auto",
                "composed_parameterized": "require_review",
                "composed_wrapped": "require_review",
                "novel": "require_review",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown category"):
        ApprovalPolicy.load(p)


def test_probe_427_policy_load_rejects_unknown_action(tmp_path) -> None:
    import yaml

    from apecx_integration.composition.approval_policy import ApprovalPolicy

    p = tmp_path / "policy.yml"
    p.write_text(
        yaml.safe_dump(
            {
                "composed_standard": "approve_immediately",  # not a real action
                "composed_parameterized": "require_review",
                "composed_wrapped": "require_review",
                "novel": "require_review",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown action"):
        ApprovalPolicy.load(p)


def test_probe_428_policy_evaluate_partitions_steps() -> None:
    """auto + review + expert must form a partition of all
    categorizations — every step lands in exactly one bucket."""
    from apecx_integration.composition.approval_policy import (
        ApprovalAction,
        ApprovalPolicy,
    )
    from apecx_integration.composition.differ import (
        CategorizedWorkflow,
        StepCategorization,
        StepCategory,
    )

    pol = ApprovalPolicy(
        mapping={
            StepCategory.COMPOSED_STANDARD: ApprovalAction.AUTO,
            StepCategory.COMPOSED_PARAMETERIZED: ApprovalAction.REQUIRE_REVIEW,
            StepCategory.COMPOSED_WRAPPED: ApprovalAction.REQUIRE_REVIEW,
            StepCategory.NOVEL: ApprovalAction.REQUIRE_EXPERT_REVIEW,
        }
    )
    cats = CategorizedWorkflow(
        categorizations=(
            StepCategorization(
                step_id="a", step_class="x", category=StepCategory.COMPOSED_STANDARD, reason="r"
            ),
            StepCategorization(
                step_id="b",
                step_class="x",
                category=StepCategory.COMPOSED_PARAMETERIZED,
                reason="r",
            ),
            StepCategorization(
                step_id="c", step_class="x", category=StepCategory.NOVEL, reason="r"
            ),
        )
    )
    decision = pol.evaluate(cats)
    auto_ids = {s.step_id for s in decision.auto_approved_steps}
    review_ids = {s.step_id for s in decision.review_required_steps}
    expert_ids = {s.step_id for s in decision.expert_review_required_steps}
    assert auto_ids == {"a"}
    assert review_ids == {"b"}
    assert expert_ids == {"c"}
    # Partition: no overlap, full coverage
    assert auto_ids.isdisjoint(review_ids)
    assert auto_ids.isdisjoint(expert_ids)
    assert review_ids.isdisjoint(expert_ids)


def test_probe_429_policy_blocks_iff_human_required() -> None:
    """`blocks` must be True iff ANY step needs a human (review
    or expert). All-auto must NOT block; presence of either
    review-tier MUST block."""
    from apecx_integration.composition.approval_policy import (
        ApprovalAction,
        ApprovalPolicy,
    )
    from apecx_integration.composition.differ import (
        CategorizedWorkflow,
        StepCategorization,
        StepCategory,
    )

    pol_auto = ApprovalPolicy(
        mapping={
            StepCategory.COMPOSED_STANDARD: ApprovalAction.AUTO,
            StepCategory.COMPOSED_PARAMETERIZED: ApprovalAction.AUTO,
            StepCategory.COMPOSED_WRAPPED: ApprovalAction.AUTO,
            StepCategory.NOVEL: ApprovalAction.AUTO,
        }
    )
    pol_block = ApprovalPolicy(
        mapping={
            StepCategory.COMPOSED_STANDARD: ApprovalAction.AUTO,
            StepCategory.COMPOSED_PARAMETERIZED: ApprovalAction.AUTO,
            StepCategory.COMPOSED_WRAPPED: ApprovalAction.AUTO,
            StepCategory.NOVEL: ApprovalAction.REQUIRE_REVIEW,
        }
    )
    cats_no_novel = CategorizedWorkflow(
        categorizations=(
            StepCategorization(
                step_id="a", step_class="x", category=StepCategory.COMPOSED_STANDARD, reason="r"
            ),
        )
    )
    cats_with_novel = CategorizedWorkflow(
        categorizations=(
            StepCategorization(
                step_id="a", step_class="x", category=StepCategory.NOVEL, reason="r"
            ),
        )
    )
    assert pol_auto.evaluate(cats_no_novel).blocks is False
    assert pol_auto.evaluate(cats_with_novel).blocks is False  # all AUTO
    assert pol_block.evaluate(cats_with_novel).blocks is True
    assert (
        pol_block.evaluate(cats_with_novel).strongest_required_action
        is ApprovalAction.REQUIRE_REVIEW
    )
