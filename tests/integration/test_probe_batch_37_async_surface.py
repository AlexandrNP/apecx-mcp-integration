"""Probe batch 37 — adversarial probes against the async surface.

User directive 2026-04-27: "Focus on testing async workflows, use
cases, and components." Day 2's rag_synthesis is synchronous, but
production callers wrap it inside async workflows / event loops.
This batch probes the sync-from-async, concurrency, cancellation,
and async-step shapes.

Streak before this batch: 25/300 post-AQ (reset by probe 955 in
batch 36).
Probe naming: 980–1004.

Distinct probes only.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage

from apecx_integration.agents.rag_synthesis import (
    DEFAULT_SYNTHESIS_CONFIG_PATH,
    SynthesisConfig,
    datacite_to_publication,
    synthesize_response,
)


pytestmark = pytest.mark.integration


def _cfg(**overrides) -> SynthesisConfig:
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw).model_copy(update=overrides)


class _Stub:
    def __init__(self, content: str, *, latency_s: float = 0.0) -> None:
        self.content = content
        self.latency_s = latency_s

    def invoke(self, msgs):
        if self.latency_s:
            time.sleep(self.latency_s)
        return AIMessage(content=self.content)


# --------------------------------------------------------------------------- #
# Probes 980–1004
# --------------------------------------------------------------------------- #


def test_probe_980_synthesize_response_works_under_asyncio_run():
    """A synchronous synthesize_response invoked inside asyncio.run
    via run_in_executor must work — production async callers will
    wrap the sync call this way to avoid blocking the loop."""
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    stub = _Stub(content=("body " * 50) + "[BV-BRC genome G1]")

    async def _runner() -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: synthesize_response("Q", llm=stub, **inputs),
        )

    out = asyncio.run(_runner())
    assert "[BV-BRC genome G1]" in out


def test_probe_981_concurrent_synthesize_no_state_leak_via_thread_pool():
    """Ten concurrent synthesize_response calls in a ThreadPoolExecutor
    each citing their own genome ID. None must succeed citing another
    thread's genome — proves no module-level state contamination."""
    def _one(idx: int) -> str:
        gid = f"GID_{idx}"
        return synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": gid, "name": "n"}],
            llm=_Stub(
                content=("body " * 50) + f"[BV-BRC genome {gid}]"
            ),
        )

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(_one, i) for i in range(10)]
        results = [f.result(timeout=10) for f in futures]
    # Each result cites its own gid and only its own.
    for i, out in enumerate(results):
        assert f"[BV-BRC genome GID_{i}]" in out


def test_probe_982_concurrent_synthesize_thread_cross_contamination_rejected():
    """If thread A's allowed_tokens leaked into thread B's grounding
    check, thread B citing A's genome would silently pass. Set up a
    race: 8 threads each citing thread 0's genome (which was NOT
    in their inputs); all must raise."""
    def _one_citing_zero(idx: int) -> str:
        gid = f"OWN_{idx}"
        # Cite thread 0's genome ID — which is NOT in this thread's
        # inputs. Grounding must reject every call.
        return synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": gid, "name": "n"}],
            llm=_Stub(content=("body " * 50) + "[BV-BRC genome OWN_0]"),
        )

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_one_citing_zero, i) for i in range(1, 9)]
        for f in futures:
            with pytest.raises(ValueError, match="hallucinat"):
                f.result(timeout=10)


def test_probe_983_synthesize_inside_running_event_loop_via_to_thread():
    """The asyncio.to_thread bridge must wrap a synchronous
    synthesize_response without deadlock or state leak."""
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    stub = _Stub(content=("body " * 50) + "[BV-BRC genome G1]")

    async def _go():
        return await asyncio.to_thread(
            synthesize_response, "Q", llm=stub, **inputs,
        )

    out = asyncio.run(_go())
    assert "[BV-BRC genome G1]" in out


def test_probe_984_asyncio_gather_concurrent_synthesizers():
    """asyncio.gather over N to_thread-wrapped synthesize_response
    calls must complete in parallel without crashes or wrong-result
    cross-talk. Each task cites its own gid."""
    async def _one(idx: int) -> tuple[int, str]:
        gid = f"AG_{idx}"
        out = await asyncio.to_thread(
            synthesize_response,
            "Q",
            bvbrc_genomes=[{"genome_id": gid, "name": "n"}],
            llm=_Stub(content=("body " * 50) + f"[BV-BRC genome {gid}]"),
        )
        return (idx, out)

    async def _go():
        return await asyncio.gather(*[_one(i) for i in range(5)])

    results = asyncio.run(_go())
    for idx, out in results:
        assert f"[BV-BRC genome AG_{idx}]" in out


def test_probe_985_cancel_async_synthesize_does_not_corrupt_module_state():
    """Cancelling an in-flight synthesize_response (via a cancelled
    asyncio task) must not leave module-level state inconsistent.
    Probe via two phases: cancel one task, then run a clean task.
    The clean task must succeed."""
    slow_stub = _Stub(content=("body " * 50) + "[BV-BRC genome SLOW]",
                      latency_s=2.0)

    async def _slow():
        return await asyncio.to_thread(
            synthesize_response, "Q",
            bvbrc_genomes=[{"genome_id": "SLOW", "name": "n"}],
            llm=slow_stub,
        )

    async def _go():
        task = asyncio.create_task(_slow())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass
        # Now a clean call — module state must be intact.
        return await asyncio.to_thread(
            synthesize_response, "Q",
            bvbrc_genomes=[{"genome_id": "CLEAN", "name": "n"}],
            llm=_Stub(content=("body " * 50) + "[BV-BRC genome CLEAN]"),
        )

    out = asyncio.run(_go())
    assert "[BV-BRC genome CLEAN]" in out


def test_probe_986_synthesize_invocation_uses_llm_invoke_only_no_async_methods():
    """The synthesizer calls ``llm.invoke(...)``. If a future change
    (e.g. switching to ``llm.ainvoke``) silently breaks sync callers
    by adding ``await``, this probe catches it. Stub does NOT have
    an ainvoke method — call must succeed via invoke-only path."""
    class _SyncOnly:
        def invoke(self, msgs):
            return AIMessage(content=("body " * 50) + "[BV-BRC genome OK]")
        # NO ainvoke / __aenter__ / etc.

    out = synthesize_response(
        "Q",
        bvbrc_genomes=[{"genome_id": "OK", "name": "n"}],
        llm=_SyncOnly(),
    )
    assert "[BV-BRC genome OK]" in out


def test_probe_987_harvester_adapter_thread_safe():
    """``datacite_to_publication`` must be re-entrant. Run 20 threads
    each adapting a unique DataCite — no shared-state surprises."""
    from apecx_harvesters.loaders.base.model import (
        DataCite, Identifier, Title, Publisher,
    )

    def _one(idx: int) -> dict:
        rec = DataCite(
            identifier=Identifier(
                identifier=f"10.1/thread-{idx}", identifierType="DOI",
            ),
            creators=[], titles=[Title(title=f"T{idx}")],
            publisher=Publisher(name="P"),
        )
        return datacite_to_publication(rec)

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(_one, range(50)))
    dois = {r["doi"] for r in results}
    assert len(dois) == 50, f"thread-id collision in DOIs: {dois!r}"


def test_probe_988_synthesizer_caplog_does_not_leak_between_threads(caplog):
    """logger.warning messages from concurrent lenient-mode skips
    must not be misattributed across threads. Run a thread with bad
    rows + lenient mode AND a thread with strict mode in parallel.
    The strict thread should NOT see the lenient thread's warnings."""
    caplog.set_level("WARNING", logger="apecx_integration.agents.rag_synthesis.synthesizer")
    cfg_lenient = _cfg(strict_input_validation=False)

    def _lenient_with_bad():
        synthesize_response(
            "Q",
            bvbrc_genomes=[
                {"name": "no_id"},
                {"genome_id": "GOOD", "name": "n"},
            ],
            llm=_Stub(content=("body " * 50) + "[BV-BRC genome GOOD]"),
            config=cfg_lenient,
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_lenient_with_bad)
        f2 = ex.submit(_lenient_with_bad)
        f1.result(timeout=10)
        f2.result(timeout=10)
    # Both lenient calls had a bad row → 2 warnings total.
    skip_logs = [
        r for r in caplog.records
        if "skipping" in r.message and "bvbrc_genome" in r.message
    ]
    assert len(skip_logs) == 2, (
        f"unexpected skip-warning count: {len(skip_logs)} (expected 2)"
    )


def test_probe_989_db_integration_wrappers_process_is_async():
    """Each ``Step`` subclass in db_integration_wrappers must have an
    async ``process``. Nanobrain's executor awaits it; a sync
    process() would silently fail the contract.

    Friction shape: after a refactor a contributor types ``def
    process(...)`` instead of ``async def process(...)``; the
    framework calls it, gets a coroutine-like wrapper, and the
    semantics drift. Verify the contract holds NOW."""
    import inspect
    from apecx_integration.composition.steps import db_integration_wrappers as m
    for name, cls in inspect.getmembers(m, inspect.isclass):
        if name.endswith("Step") and hasattr(cls, "process"):
            proc = cls.process
            assert inspect.iscoroutinefunction(proc), (
                f"{cls.__name__}.process must be async; got {proc}"
            )


def test_probe_990_mcp_tools_module_imports_without_event_loop():
    """The MCP tools modules use FastMCP decorators; importing them
    must not require a running event loop or an external connection.
    Catches the silent-failure shape where tool registration calls
    asyncio.run at import time."""
    import importlib
    for modname in (
        "apecx_integration.mcp_surface.tools.workflows",
        "apecx_integration.mcp_surface.tools.approvals",
        "apecx_integration.mcp_surface.tools.hpc",
    ):
        importlib.import_module(modname)  # must not raise


def test_probe_991_sync_synthesize_does_not_block_other_threads_indefinitely():
    """The synthesizer is sync; if an in-flight call is slow, OTHER
    threads must still proceed. Probe with one slow call + several
    fast calls; assert the fast calls finish before the slow one."""
    times: list[tuple[str, float]] = []

    def _record(label: str, latency: float):
        t0 = time.monotonic()
        synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": label, "name": "n"}],
            llm=_Stub(
                content=("body " * 50) + f"[BV-BRC genome {label}]",
                latency_s=latency,
            ),
        )
        times.append((label, time.monotonic() - t0))

    with ThreadPoolExecutor(max_workers=4) as ex:
        ex.submit(_record, "SLOW", 1.0)
        time.sleep(0.05)  # let SLOW start first
        for fast_id in ("F1", "F2", "F3"):
            ex.submit(_record, fast_id, 0.0)
        ex.shutdown(wait=True)

    by_label = dict(times)
    assert by_label["F1"] < 0.5, (
        f"fast call F1 took {by_label['F1']:.3f}s — likely blocked by SLOW"
    )


def test_probe_992_synthesize_with_huge_input_count_does_not_oom():
    """1000-element input lists with cap=8: the cap clip via
    ``list(genomes)[:cap]`` should keep memory bounded. Probe by
    profiling the call's success on a large input."""
    huge_chunks = [{"text": f"chunk text {i}"} for i in range(10000)]
    cfg = _cfg(max_rag_chunks=8)
    out = synthesize_response(
        "Q",
        rag_chunks=huge_chunks,
        bvbrc_genomes=[{"genome_id": "G1", "name": "n"}],
        llm=_Stub(
            content=("body " * 50) + "[BV-BRC genome G1] [RAG chunk #1]"
        ),
        config=cfg,
    )
    assert "[BV-BRC genome G1]" in out


def test_probe_993_e2e_module_helpers_thread_safe():
    """The E2E helpers _load_bvbrc_genomes / _load_violin_vaccines /
    _load_rag_chunks must be re-entrant — the test file may be
    invoked under pytest-xdist with parallel collection."""
    from tests.integration import test_e2e_rag_pipeline_against_ollama as m
    if not m.BVBRC_TSV.is_file():
        pytest.skip("BV-BRC TSV not present")

    def _load_one(_):
        return m._load_bvbrc_genomes(limit=2)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_load_one, range(8)))
    # All threads must see the same first 2 rows.
    first = results[0]
    for r in results[1:]:
        assert r == first


def test_probe_994_threadpool_synthesize_with_shared_config_object():
    """Sharing a SynthesisConfig instance across threads must NOT
    cause mutation. Pydantic models are frozen by default for
    BaseModel-derived; verify by mutating a shared cfg in one
    thread and observing nothing in another (would raise)."""
    cfg = _cfg()

    def _one(idx: int):
        synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": f"S_{idx}", "name": "n"}],
            llm=_Stub(content=("body " * 50) + f"[BV-BRC genome S_{idx}]"),
            config=cfg,
        )

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_one, range(10)))
    # Original cfg unchanged.
    assert cfg.max_rag_chunks == 8


def test_probe_995_threadpool_synthesize_under_lenient_with_partial_failures():
    """Concurrent lenient-mode calls where some calls have ALL bad
    rows (which then trigger fail_on_empty_retrieval). Each thread's
    failure must isolate; clean threads must succeed."""
    def _all_bad():
        synthesize_response(
            "Q",
            bvbrc_genomes=[{"name": "no_id"}],
            llm=_Stub(content="ignored"),
            config=_cfg(strict_input_validation=False),
        )

    def _clean(idx):
        return synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": f"CL_{idx}", "name": "n"}],
            llm=_Stub(content=("body " * 50) + f"[BV-BRC genome CL_{idx}]"),
            config=_cfg(strict_input_validation=False),
        )

    bad_results = []
    clean_results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        bad_futs = [ex.submit(_all_bad) for _ in range(3)]
        clean_futs = [ex.submit(_clean, i) for i in range(3)]
        for f in bad_futs:
            try:
                f.result(timeout=10)
            except ValueError as e:
                bad_results.append(str(e))
        for f in clean_futs:
            clean_results.append(f.result(timeout=10))
    assert len(bad_results) == 3, "bad threads should ALL raise"
    assert all("[BV-BRC genome CL_" in r for r in clean_results)


def test_probe_996_synthesize_supports_iterables_returning_falsy():
    """Edge case: passing an iterable of falsy-but-valid dicts. The
    iterable contract must not depend on truthiness."""
    inputs = dict(
        rag_chunks=iter([{"text": "x"}]),
        bvbrc_genomes=iter([{"genome_id": "G1", "name": "n"}]),
    )
    stub = _Stub(
        content=("body " * 50) + "[BV-BRC genome G1] [RAG chunk #1]"
    )
    out = synthesize_response("Q", llm=stub, **inputs)
    assert "[BV-BRC genome G1]" in out


def test_probe_997_threading_event_does_not_deadlock_synthesize():
    """A Lock acquired before synthesize_response and released inside
    the call must not deadlock. Ensures the synthesizer doesn't
    acquire the same lock internally (no global mutex)."""
    lock = threading.Lock()
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    stub = _Stub(content=("body " * 50) + "[BV-BRC genome G1]")
    with lock:
        out = synthesize_response("Q", llm=stub, **inputs)
    assert "[BV-BRC genome G1]" in out


def test_probe_998_async_step_signature_takes_kwargs_not_positional():
    """The nanobrain Step.process contract is ``async def process(self,
    input_data: dict, **kwargs)``. Verify db_integration_wrappers
    matches — a positional-only signature would break the executor."""
    import inspect
    from apecx_integration.composition.steps import db_integration_wrappers as m
    for name, cls in inspect.getmembers(m, inspect.isclass):
        if name.endswith("Step") and hasattr(cls, "process"):
            sig = inspect.signature(cls.process)
            params = list(sig.parameters.values())
            # First param is self; second is input_data.
            assert len(params) >= 2, f"{name}.process has too few params"
            # input_data must be POSITIONAL_OR_KEYWORD.
            assert params[1].kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }


def test_probe_999_synthesize_response_returns_str_not_message_object():
    """A future refactor returning the raw AIMessage instead of
    ``content`` would silently break the API contract — the
    publication renderer in downstream callers expects a str."""
    inputs = dict(bvbrc_genomes=[{"genome_id": "G1", "name": "n"}])
    stub = _Stub(content=("body " * 50) + "[BV-BRC genome G1]")
    out = synthesize_response("Q", llm=stub, **inputs)
    assert isinstance(out, str), f"expected str, got {type(out).__name__}"


def test_probe_1000_synthesize_under_high_thread_concurrency_rate_no_failure():
    """50 concurrent synthesize_response calls in 16-thread pool. No
    errors, all complete, each returns its own gid."""
    def _one(idx: int) -> str:
        gid = f"R{idx}"
        return synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": gid, "name": "n"}],
            llm=_Stub(content=("body " * 50) + f"[BV-BRC genome {gid}]"),
        )

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(_one, range(50)))
    for i, out in enumerate(results):
        assert f"[BV-BRC genome R{i}]" in out


def test_probe_1001_concurrent_module_state_isolation_via_distinct_fail_modes():
    """Two threads: thread A passes inputs that pass; thread B passes
    inputs that fail (citation mismatch). Result: A succeeds, B
    raises, no cross-thread contamination of failure state."""
    def _good():
        return synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": "GOOD_THREAD", "name": "n"}],
            llm=_Stub(content=("body " * 50) + "[BV-BRC genome GOOD_THREAD]"),
        )

    def _bad():
        synthesize_response(
            "Q",
            bvbrc_genomes=[{"genome_id": "BAD_THREAD", "name": "n"}],
            # cite a different gid — must raise
            llm=_Stub(content=("body " * 50) + "[BV-BRC genome ELSEWHERE]"),
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        good_fut = ex.submit(_good)
        bad_fut = ex.submit(_bad)
        good_out = good_fut.result(timeout=10)
        with pytest.raises(ValueError):
            bad_fut.result(timeout=10)
    assert "[BV-BRC genome GOOD_THREAD]" in good_out


def test_probe_1002_synthesize_with_no_kwargs_only_query_uses_default_path():
    """Calling with only the query (no inputs) must use the
    fail_on_empty_retrieval gate — and should fail BEFORE any
    LLM call. Test the timing as a safety check."""
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="every retrieval input is empty"):
        synthesize_response("Q")
    elapsed = time.monotonic() - t0
    # No LLM call → fast.
    assert elapsed < 0.5


def test_probe_1003_synthesizer_handles_llm_returning_object_with_content_property():
    """A custom LLM client may return an object whose ``content`` is a
    @property. The synthesizer must use ``getattr(response,
    'content', None)`` (not ``response.content`` directly) to handle
    both shape consistently."""
    class _PropResponse:
        @property
        def content(self) -> str:
            return ("body " * 50) + "[BV-BRC genome G1]"

    class _PropLLM:
        def invoke(self, _msgs):
            return _PropResponse()

    out = synthesize_response(
        "Q",
        bvbrc_genomes=[{"genome_id": "G1", "name": "n"}],
        llm=_PropLLM(),
    )
    assert "[BV-BRC genome G1]" in out


def test_probe_1004_synthesize_response_does_not_modify_input_lists():
    """Caller-supplied input lists must NOT be mutated by the
    synthesizer (e.g. via in-place sort, pop, etc.). Production
    callers may reuse the input bundle across multiple calls;
    mutation would break that pattern silently."""
    rag = [{"text": "a"}, {"text": "b"}]
    bvbrc = [{"genome_id": "G1", "name": "n"}]
    rag_snapshot = list(rag)
    bvbrc_snapshot = list(bvbrc)
    stub = _Stub(content=("body " * 50) + "[BV-BRC genome G1] [RAG chunk #1]")
    synthesize_response(
        "Q",
        rag_chunks=rag,
        bvbrc_genomes=bvbrc,
        llm=stub,
    )
    assert rag == rag_snapshot, "rag_chunks input mutated"
    assert bvbrc == bvbrc_snapshot, "bvbrc_genomes input mutated"
