#!/usr/bin/env python3
"""In-container job harness for a sandboxed novel step (#1c Phase 1).

Runs INSIDE the hardened sandbox container (never imported into the host process). Reads a job.json,
materializes the composer's ``novel_source`` as a module, ``from_config``-builds the target BaseStep,
runs its ``process(input_data)``, and writes a result envelope to result.json:

    {"ok": true,  "output": <dict returned by process>}
    {"ok": false, "error_type": ..., "note": ..., "traceback": ...}

Top-level try/except-emit (mirrors _pymol_job.py) so a failing novel step surfaces a real traceback in
result.json rather than crashing the container silently. argv: <job.json> <result.json>.

The host side (SandboxedNovelStep.process) serializes the step's inputs into job.json and reads the
output back — inputs/outputs are plain JSON objects (nanobrain hands process() the raw stored DU
values, not DataUnit objects), so the JSON boundary is faithful.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import tempfile
import traceback
from typing import Any


def _load_target_class(source: str, class_name: str):
    """Write the novel source to a temp module and return its ``class_name`` attribute."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", dir="/tmp", delete=False) as fp:
        fp.write(source)
        module_path = fp.name
    spec = importlib.util.spec_from_file_location("_apecx_novel_module", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not load novel module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, class_name):
        raise AttributeError(f"novel source does not define class {class_name!r}")
    return getattr(module, class_name)


async def _build_and_run(cls, config: dict[str, Any], input_data: dict[str, Any]) -> Any:
    # from_config is the mandatory nanobrain constructor; tolerate a sync OR async implementation.
    built = cls.from_config(config)
    step = await built if inspect.isawaitable(built) else built
    return await step.process(input_data)


def run(job: dict[str, Any]) -> Any:
    cls = _load_target_class(job["novel_source"], job["target_class_name"])
    config = {"name": job.get("step_name", "novel_step"), **(job.get("config") or {})}
    return asyncio.run(_build_and_run(cls, config, job.get("input_data") or {}))


def main() -> int:
    job_path, result_path = sys.argv[1], sys.argv[2]
    try:
        with open(job_path, encoding="utf-8") as fp:
            job = json.load(fp)
        payload: dict[str, Any] = {"ok": True, "output": run(job)}
    except Exception as exc:  # noqa: BLE001 — any failure must surface as a structured envelope
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "note": str(exc)[:500],
            "traceback": traceback.format_exc()[-4000:],
        }
    with open(result_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
