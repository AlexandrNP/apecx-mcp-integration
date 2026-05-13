"""Verifier for the ``step_uppercase`` nanobrain-native problem.

Contract: ``verify(candidate_code: str, tmp_path: Path) -> tuple[bool, str]``
returns (passed, error_message). The runner-side adapter packages
this into the benchmark scoring layer.

We do REAL execution: write the candidate to a module, write a tiny
YAML config alongside, load via ``BaseStep.from_config``, invoke
``process`` on real input. No mocks. If a future framework change
breaks the ``BaseStep.from_config`` path, this verifier surfaces the
break loud.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path


def verify(candidate_code: str, tmp_path: Path) -> tuple[bool, str]:
    """Return ``(passed, error_message)`` for the candidate code.

    Steps:
      1. Write candidate to ``tmp_path/upper_step_module.py``.
      2. Write a YAML at ``tmp_path/step.yml`` referencing the class.
      3. Load via ``BaseStep.from_config``.
      4. Run process({"text": "hello"}) and check output == "HELLO".
    """
    module_path = tmp_path / "upper_step_module.py"
    module_path.write_text(candidate_code)

    spec = importlib.util.spec_from_file_location("upper_step_module", str(module_path))
    if spec is None or spec.loader is None:
        return False, "could not build importlib spec for candidate module"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        return False, f"candidate module failed to import: {type(e).__name__}: {e}"

    if not hasattr(module, "UpperStep"):
        return False, "candidate module does not define ``UpperStep``"
    cls = module.UpperStep

    yaml_path = tmp_path / "step.yml"
    yaml_path.write_text("class: '__main__.UpperStep'\nname: upper_test\n")
    try:
        # ``BaseStep.from_config`` looks up the class via its
        # configured class string — we set ``class:`` to a sentinel
        # then override by passing the resolved class via
        # ``_allow_direct_instantiation`` backdoor isn't quite right
        # either. Simplest stable path: instantiate via from_config
        # with the actual module path embedded in the YAML.
        yaml_path.write_text("class: 'upper_step_module.UpperStep'\nname: upper_test\n")
        # Add tmp_path to sys.path so the class import resolves.
        import sys  # noqa: PLC0415

        if str(tmp_path) not in sys.path:
            sys.path.insert(0, str(tmp_path))
        try:
            from nanobrain.core.step import BaseStep  # noqa: PLC0415

            step = BaseStep.from_config(str(yaml_path))
        finally:
            sys.path.remove(str(tmp_path))
    except Exception as e:
        return False, (
            f"BaseStep.from_config failed: {type(e).__name__}: {e}. "
            f"Likely the candidate's config class is missing extra='forbid' "
            f"or the class path in the YAML doesn't resolve."
        )

    try:
        result = asyncio.run(step.process({"text": "hello"}))
    except Exception as e:
        return False, f"step.process raised: {type(e).__name__}: {e}"

    if not isinstance(result, dict):
        return False, f"process() returned non-dict {type(result).__name__}: {result!r}"
    if result.get("output") != "HELLO":
        return False, (
            f"expected output='HELLO', got {result.get('output')!r}; full result: {result!r}"
        )

    # Optional sanity check: confirm the candidate does NOT override
    # execute(). The framework would have raised at init if it did,
    # so reaching here without a from_config error implies compliance.
    # We additionally surface a clear message if the class accidentally
    # exposes execute as a non-inherited method.
    if "execute" in cls.__dict__:
        return False, "candidate's class overrides execute() — forbidden by framework"

    return True, ""


__all__ = ["verify"]
