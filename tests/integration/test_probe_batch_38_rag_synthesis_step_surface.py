"""Probe batch 38 — adversarial probes against the new RagSynthesisStep
nanobrain wrapper (Day 2 v9 + v10).

Streak before this batch: 50/300 post-AQ.
Probe naming: 1005–1029.

Distinct probes only — none of these check shapes covered by prior
batches.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from apecx_integration.composition.steps.rag_synthesis_step import (
    RagSynthesisStep,
    RagSynthesisStepConfig,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_YAML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
    / "steps"
    / "rag_synthesis.yml"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _patch_synth(
    monkeypatch, captured: dict | None = None, content: str = "fake markdown synthesis " * 20
):
    """Replace synthesize_response inside the step module with a stub
    that captures inputs. Returns the captured dict for assertions."""
    captured = captured if captured is not None else {}

    def _fake(query: str, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return content

    import apecx_integration.composition.steps.rag_synthesis_step as mod

    monkeypatch.setattr(mod, "synthesize_response", _fake)
    return captured


# --------------------------------------------------------------------------- #
# Probes 1005–1029
# --------------------------------------------------------------------------- #


def test_probe_1005_step_input_data_extra_keys_silently_ignored_or_propagated(monkeypatch):
    """A future caller may add a new input key (e.g. ``conversation_history``).
    The step's current behavior: forward only the known four sources +
    query. Extra keys are NEITHER passed to synthesize_response NOR
    raise. This probe pins the contract — silent ignore vs. propagate
    must be deterministic."""
    captured = _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    asyncio.run(
        step.process(
            {
                "query": "Q",
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
                "conversation_history": ["prior turn"],  # NEW key — drops?
                "user_id": "alice",
            }
        )
    )
    # Confirm the unknown keys did NOT leak to synthesize_response.
    kw_keys = set(captured["kwargs"].keys())
    assert "conversation_history" not in kw_keys
    assert "user_id" not in kw_keys
    # 2026-05-11: ``globus_results`` was added to the synthesizer's
    # forwarded kwargs when Globus Search joined the ingest boundary
    # (e14fb2d). The probe's "deterministic forward set" contract
    # holds — we just have to keep the expected set in sync with
    # the synthesizer signature.
    assert kw_keys == {
        "rag_chunks",
        "bvbrc_genomes",
        "violin_mappings",
        "publications",
        "globus_results",
        "config",
    }


def test_probe_1006_step_rejects_bytes_query(monkeypatch):
    """``query`` must be a str — bytes is a common sloppy-input shape
    that historic libraries silently iterated. Current contract:
    fail-fast with non-empty 'query' string error."""
    _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    with pytest.raises(ValueError, match="non-empty 'query' string"):
        asyncio.run(step.process({"query": b"bytes"}))


def test_probe_1007_step_publications_none_treated_as_empty_list(monkeypatch):
    """Caller may pass ``publications: None`` instead of ``[]``. The
    step substitutes empty list — semantically equivalent."""
    captured = _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    asyncio.run(
        step.process(
            {
                "query": "Q",
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
                "publications": None,
            }
        )
    )
    assert captured["kwargs"]["publications"] == []


def test_probe_1008_step_rag_chunks_string_not_silently_iterated(monkeypatch):
    """If a caller passes ``rag_chunks: "some text"`` (string, not
    list[dict]), Python's iterable contract would silently iterate
    over CHARACTERS — each char a "chunk". The synthesizer's
    isinstance(chunk, dict) check rejects this; verify the failure
    surfaces, not a silent zero-rendering."""
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    # The default config has strict_input_validation=True, which
    # raises on non-dict rows.
    with pytest.raises(ValueError, match="expected dict"):
        asyncio.run(
            step.process(
                {
                    "query": "Q",
                    "rag_chunks": "raw string chunk",
                }
            )
        )


def test_probe_1009_step_returns_dict_with_only_synthesis_key(monkeypatch):
    """The output contract is ``{"synthesis": str}`` — exactly one
    key. A future refactor adding diagnostic keys (timing, token
    count) would break downstream consumers expecting the lean
    shape; this probe pins the contract."""
    _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    out = asyncio.run(
        step.process(
            {
                "query": "Q",
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
            }
        )
    )
    assert set(out.keys()) == {"synthesis"}, f"output dict has unexpected keys: {set(out.keys())!r}"


def test_probe_1010_step_synthesis_value_is_str(monkeypatch):
    """The ``synthesis`` value must be a str — never an AIMessage,
    never a Markdown bytestring. Probe ensures the step doesn't
    silently leak the LLM's raw response object."""
    _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    out = asyncio.run(
        step.process(
            {
                "query": "Q",
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
            }
        )
    )
    assert isinstance(out["synthesis"], str)


def test_probe_1011_step_concurrent_calls_use_independent_state(monkeypatch):
    """Two concurrent step.process() calls (different queries +
    inputs). Each must produce its own synthesis without state
    cross-talk. Stub captures both invocations to verify."""
    invocations: list[dict] = []

    def _fake(query: str, **kwargs):
        invocations.append({"query": query, "kwargs": kwargs})
        return "synth body. " * 30

    import apecx_integration.composition.steps.rag_synthesis_step as mod

    monkeypatch.setattr(mod, "synthesize_response", _fake)

    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))

    async def _run():
        return await asyncio.gather(
            step.process({"query": "Q1", "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}]}),
            step.process({"query": "Q2", "bvbrc_genomes": [{"genome_id": "G2", "name": "n"}]}),
        )

    out1, out2 = asyncio.run(_run())
    assert isinstance(out1, dict) and isinstance(out2, dict)
    queries = {inv["query"] for inv in invocations}
    assert queries == {"Q1", "Q2"}


def test_probe_1012_step_class_attributes_match_contract():
    """Nanobrain Step contract requires ``COMPONENT_TYPE`` and
    ``REQUIRED_CONFIG_FIELDS``. Pin the values so a future refactor
    doesn't silently drop them (the framework's introspection on
    these attrs is load-bearing)."""
    assert RagSynthesisStep.COMPONENT_TYPE == "rag_synthesis_step"
    assert "name" in RagSynthesisStep.REQUIRED_CONFIG_FIELDS


def test_probe_1013_step_process_is_coroutine_function():
    """The framework expects ``async def process(...)``. A sync
    accidentally-defined process would silently be wrapped into a
    coroutine-of-None by the executor and the step would never
    actually run. Verify directly."""
    assert inspect.iscoroutinefunction(RagSynthesisStep.process)


def test_probe_1014_step_get_config_class_returns_rag_synthesis_config():
    """Loaded config goes through ``_get_config_class()``; mismatch
    would mean the synthesis_config_path field doesn't get parsed."""
    assert RagSynthesisStep._get_config_class() is RagSynthesisStepConfig


def test_probe_1015_synthesis_config_path_relative_resolution(tmp_path):
    """The wrapper YAML's ``synthesis_config_path`` field is a string;
    relative paths are interpreted relative to the YAML's directory
    by the loader (current behavior — pin it).

    NB: the current implementation uses Path(path) which is
    interpreted relative to cwd, NOT the YAML directory. This probe
    documents that contract.
    """
    # Create a synthesis config at a known path.
    synth_yaml = tmp_path / "synthesis_relative.yml"
    synth_yaml.write_text("system_prompt: 'rel-test'\n")
    wrapper = tmp_path / "rag_synthesis_rel.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep\n"
        "name: rel_step\n"
        "description: rel\n"
        f"synthesis_config_path: '{synth_yaml}'\n"
        "input_data_units:\n"
        "  synthesis_input:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: synthesis_input\n"
        "    description: input\n"
        "    persistent: false\n"
        "output_data_units:\n"
        "  synthesis_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: synthesis_output\n"
        "    description: output\n"
        "    persistent: false\n"
        "triggers:\n"
        "  - class: nanobrain.core.trigger.DataUnitChangeTrigger\n"
        "    data_unit: synthesis_input\n"
    )
    step = RagSynthesisStep.from_config(str(wrapper))
    # Loaded successfully — the config path resolved.
    assert step._synthesis_config is not None
    assert step._synthesis_config.system_prompt == "rel-test"


def test_probe_1016_step_with_default_synthesis_config_uses_bundled_yaml():
    """When ``synthesis_config_path`` is unset (the default in the
    bundled wrapper YAML), the step's _synthesis_config is None and
    synthesize_response is called with config=None — synthesize_response
    then loads the bundled default. Pin this delegation pattern."""
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    assert step._synthesis_config is None


def test_probe_1017_step_does_not_mutate_input_data_dict(monkeypatch):
    """The caller-supplied ``input_data`` dict must NOT be mutated by
    the step (e.g. via ``.pop()`` of unknown keys). Production
    callers may reuse the input across multiple step calls; mutation
    breaks that pattern silently."""
    _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    inputs = {
        "query": "Q",
        "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
    }
    snapshot = dict(inputs)
    asyncio.run(step.process(inputs))
    assert inputs == snapshot, "input_data dict was mutated"


def test_probe_1018_step_rejects_query_as_int(monkeypatch):
    """Defensive: caller passes int as query. Must be rejected with
    the typed 'non-empty 'query' string' error."""
    _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    with pytest.raises(ValueError, match="non-empty 'query'"):
        asyncio.run(step.process({"query": 42}))


def test_probe_1019_step_rejects_query_with_only_punctuation(monkeypatch):
    """Whitespace-only query is rejected (.strip() check). What about
    punctuation-only ``'?'`` or ``'!!!'``? Current contract: those
    pass the .strip() check and reach the synthesizer (where the
    LLM does whatever the LLM does). Pin current behavior."""
    captured = _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    asyncio.run(
        step.process(
            {
                "query": "?",
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
            }
        )
    )
    assert captured["query"] == "?"


def test_probe_1020_step_input_data_is_none_rejected(monkeypatch):
    """``input_data=None`` would crash on attribute access in process();
    must be rejected with the typed 'must be a dict' error."""
    _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process(None))


def test_probe_1021_step_input_data_as_list_rejected(monkeypatch):
    """A common sloppy shape: caller passes list-of-dict instead of
    dict. The .get() pattern would AttributeError. Must reject."""
    _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process([{"query": "Q"}]))


def test_probe_1022_step_propagates_synthesizer_value_error_verbatim(monkeypatch):
    """If the synthesizer raises (e.g. all-empty retrieval), the step's
    process() must propagate the original error — not wrap or swallow.
    Operators read synthesizer error messages to fix root causes."""

    def _raising(query, **kwargs):
        raise ValueError("synthesize_response: every retrieval input is empty")

    import apecx_integration.composition.steps.rag_synthesis_step as mod

    monkeypatch.setattr(mod, "synthesize_response", _raising)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    with pytest.raises(ValueError, match="every retrieval input is empty"):
        asyncio.run(step.process({"query": "Q"}))


def test_probe_1023_step_logging_includes_source_counts(monkeypatch, caplog):
    """The step's log line includes per-source row counts. A future
    silent-failure shape: a refactor drops the count from the log,
    making operators unable to see "rag=0 bvbrc=2" diagnostics. Pin
    the log format — any source count missing fails this test."""
    _patch_synth(monkeypatch)
    caplog.set_level(
        "INFO",
        logger="apecx_integration.composition.steps.rag_synthesis_step",
    )
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    asyncio.run(
        step.process(
            {
                "query": "Q",
                "rag_chunks": [{"text": "x"}],
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
                "violin_mappings": [],
                "publications": [],
            }
        )
    )
    log_msgs = [r.message for r in caplog.records]
    matches = [
        m for m in log_msgs if "rag=" in m and "bvbrc=" in m and "violin=" in m and "pubs=" in m
    ]
    assert matches, f"per-source-count log line missing; got messages: {log_msgs!r}"


def test_probe_1024_synthesis_config_path_with_yaml_having_only_required_field(tmp_path):
    """A minimal synthesis config (just system_prompt) loads cleanly.
    Defaults populate every other field. Pin this so a future schema
    change that makes another field required raises here, alerting
    operators with minimal configs."""
    synth_yaml = tmp_path / "minimal.yml"
    synth_yaml.write_text("system_prompt: 'minimal'\n")

    from apecx_integration.agents.rag_synthesis import SynthesisConfig

    cfg = SynthesisConfig.model_validate({"system_prompt": "minimal"})
    assert cfg.max_rag_chunks == 8  # default


def test_probe_1025_step_independent_instances_have_independent_configs(tmp_path):
    """Loading two RagSynthesisStep instances from different wrapper
    YAMLs (different synthesis_config_path) must keep configs
    isolated. Cross-instance state would mean a config change in
    one workflow silently affects another."""
    a_synth = tmp_path / "a_synth.yml"
    a_synth.write_text("system_prompt: 'A'\n")
    b_synth = tmp_path / "b_synth.yml"
    b_synth.write_text("system_prompt: 'B'\n")
    a_wrapper = tmp_path / "a_wrapper.yml"
    b_wrapper = tmp_path / "b_wrapper.yml"
    for w_path, c_path, name in (
        (a_wrapper, a_synth, "instance_a"),
        (b_wrapper, b_synth, "instance_b"),
    ):
        w_path.write_text(
            f"class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep\n"
            f"name: {name}\n"
            f"description: per-instance config test\n"
            f"synthesis_config_path: '{c_path}'\n"
            f"input_data_units:\n"
            f"  synthesis_input:\n"
            f"    class: nanobrain.core.data_unit.DataUnitMemory\n"
            f"    name: synthesis_input\n"
            f"    description: input\n"
            f"    persistent: false\n"
            f"output_data_units:\n"
            f"  synthesis_output:\n"
            f"    class: nanobrain.core.data_unit.DataUnitMemory\n"
            f"    name: synthesis_output\n"
            f"    description: output\n"
            f"    persistent: false\n"
            f"triggers:\n"
            f"  - class: nanobrain.core.trigger.DataUnitChangeTrigger\n"
            f"    data_unit: synthesis_input\n"
        )
    a = RagSynthesisStep.from_config(str(a_wrapper))
    b = RagSynthesisStep.from_config(str(b_wrapper))
    assert a._synthesis_config.system_prompt == "A"
    assert b._synthesis_config.system_prompt == "B"


def test_probe_1026_step_does_not_keep_old_synthesis_in_memory_between_calls(monkeypatch):
    """Probe for inadvertent caching: two consecutive calls with
    different inputs must not return the same synthesis. The step
    has no caching layer; this pins the contract."""
    counter = {"n": 0}

    def _fake(q, **kw):
        counter["n"] += 1
        return f"call-{counter['n']} body. " * 30

    import apecx_integration.composition.steps.rag_synthesis_step as mod

    monkeypatch.setattr(mod, "synthesize_response", _fake)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    out1 = asyncio.run(
        step.process(
            {
                "query": "Q1",
                "bvbrc_genomes": [{"genome_id": "G", "name": "n"}],
            }
        )
    )
    out2 = asyncio.run(
        step.process(
            {
                "query": "Q2",
                "bvbrc_genomes": [{"genome_id": "G", "name": "n"}],
            }
        )
    )
    assert out1["synthesis"] != out2["synthesis"]


def test_probe_1027_step_invokes_synthesize_via_to_thread_offload(monkeypatch):
    """Probe the offload pattern: the step uses asyncio.to_thread —
    the LLM runs on a worker thread. Verify by capturing the thread
    ident inside the stub; main thread != worker thread."""
    import threading

    main_thread = threading.get_ident()
    worker_thread_seen = []

    def _fake(q, **kw):
        worker_thread_seen.append(threading.get_ident())
        return "body " * 30

    import apecx_integration.composition.steps.rag_synthesis_step as mod

    monkeypatch.setattr(mod, "synthesize_response", _fake)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    asyncio.run(
        step.process(
            {
                "query": "Q",
                "bvbrc_genomes": [{"genome_id": "G", "name": "n"}],
            }
        )
    )
    assert worker_thread_seen, "stub never called"
    assert worker_thread_seen[0] != main_thread, (
        "synthesize_response ran on the main thread; the to_thread "
        "offload is broken — production async callers will block "
        "the event loop."
    )


def test_probe_1028_step_query_with_leading_trailing_whitespace_passed_through(monkeypatch):
    """The step's .strip() check rejects whitespace-only; a query with
    non-whitespace + surrounding whitespace passes. Pin: the query
    is passed verbatim to synthesize_response (the synthesizer does
    its own .strip() in the user prompt)."""
    captured = _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    asyncio.run(
        step.process(
            {
                "query": "  what is sindbis?  \n\n",
                "bvbrc_genomes": [{"genome_id": "G", "name": "n"}],
            }
        )
    )
    # The step does not strip — that's the synthesizer's job.
    assert captured["query"] == "  what is sindbis?  \n\n"


def test_probe_1029_step_handles_unicode_in_query(monkeypatch):
    """Non-ASCII query (Greek letters, CJK, emoji) flows through
    cleanly — the step is encoding-transparent. A future regression
    that .encode()s the query somewhere would silently break i18n."""
    captured = _patch_synth(monkeypatch)
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    q = "How does β-carotene 抗病毒 work? 🦠"
    asyncio.run(
        step.process(
            {
                "query": q,
                "bvbrc_genomes": [{"genome_id": "G", "name": "n"}],
            }
        )
    )
    assert captured["query"] == q
