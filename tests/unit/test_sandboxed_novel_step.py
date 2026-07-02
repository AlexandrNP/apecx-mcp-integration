"""Unit tests for SandboxedNovelStep (#1c Phase 0).

The step runs untrusted composer-generated Python inside a hardened Docker sandbox. These tests
build it via ``from_config`` (which validates ``process`` is async + ``name`` is accepted at init)
and exercise ``process()`` WITHOUT a real Docker daemon by monkeypatching the isolated
``_run_container`` helper — the fake writes the harness's result-envelope to the /out mount that the
argv encodes, exactly as the in-container harness would. NO real-docker dependency here; the gated
end-to-end test against a real image is separate.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest
import yaml

from apecx_integration.composition.steps.sandboxed_novel_step import (
    _EXECUTE_ENV_VAR,
    SandboxedNovelStep,
)

_NOVEL_SOURCE = "# novel step source placeholder\nclass Foo:\n    pass\n"


def _stage(tmp_path: Path, **overrides) -> SandboxedNovelStep:
    cfg = {
        "name": "novel_test",
        "novel_source": _NOVEL_SOURCE,
        "target_class_name": "Foo",
        **overrides,
    }
    p = tmp_path / "s.yml"
    p.write_text(yaml.safe_dump(cfg))
    return SandboxedNovelStep.from_config(str(p))


def _mount_source(argv: list[str], target: str) -> Path:
    """Extract the host source path of the bind mount whose container target is ``target``."""
    for tok in argv:
        segments = tok.split(",")
        if segments[0] == "type=bind" and f"target={target}" in segments:
            src = next(s[len("source=") :] for s in segments if s.startswith("source="))
            return Path(src)
    raise AssertionError(f"no bind mount with target={target} in argv: {argv}")


def test_from_config_constructs_and_process_is_async(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "novel_test"
    # from_config init raises FAIL-FAST if process() is not async; reaching here proves it, but be
    # explicit about the contract.
    assert inspect.iscoroutinefunction(step.process)


def test_process_returns_output_and_writes_faithful_job_json(tmp_path, monkeypatch):
    monkeypatch.setenv(_EXECUTE_ENV_VAR, "1")
    step = _stage(tmp_path)
    captured: dict = {}

    async def fake_run(argv):
        input_dir = _mount_source(argv, "/work")
        output_dir = _mount_source(argv, "/out")
        captured["job"] = json.loads((input_dir / "job.json").read_text())
        (output_dir / "result.json").write_text(json.dumps({"ok": True, "output": {"r": 42}}))
        return 0, ""

    monkeypatch.setattr(step, "_run_container", fake_run)

    out = asyncio.run(step.process({"seq": "ACGT"}))
    assert out == {"r": 42}

    job = captured["job"]
    assert job["novel_source"] == _NOVEL_SOURCE
    assert job["target_class_name"] == "Foo"
    assert job["step_name"] == "novel_test"
    assert job["input_data"] == {"seq": "ACGT"}


def test_process_raises_on_error_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv(_EXECUTE_ENV_VAR, "1")
    step = _stage(tmp_path)

    async def fake_run(argv):
        output_dir = _mount_source(argv, "/out")
        (output_dir / "result.json").write_text(
            json.dumps(
                {
                    "ok": False,
                    "error_type": "ValueError",
                    "note": "boom",
                    "traceback": "Traceback ...\nValueError: boom\n",
                }
            )
        )
        return 1, ""

    monkeypatch.setattr(step, "_run_container", fake_run)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(step.process({"x": 1}))


def test_process_gate_refuses_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv(_EXECUTE_ENV_VAR, raising=False)
    step = _stage(tmp_path)
    with pytest.raises(RuntimeError, match=_EXECUTE_ENV_VAR):
        asyncio.run(step.process({"x": 1}))
