"""TdrIterationStep — one iteration of the TDR refine loop, framework-native.

One ``process()`` call performs the per-iteration TDR work:
1. Builds the writer input from the inbound envelope (detects initial
   vs revision shape; the back-edge through ``LoopController`` wraps
   the prior envelope in a ``{payload, allow_continue, ...}`` shell
   that we unwrap here).
2. Calls the bundled ``CodeWriteStep`` to generate (or revise) code.
3. Calls the bundled ``IsolatedPyExecStep`` to execute against the
   test code (APECX_CODE_EXEC=1 gate — operator opt-in).
4. Formats the test-failure stderr into a critique string for the
   next iteration's revision call.
5. Emits an envelope carrying ALL state needed for either the next
   iteration (revision inputs + persistent context) or the workflow
   output (final code_source + exec_result).

Why this design — one Step instead of an adapter chain
=======================================================

The framework demonstration here is the **cycle topology** —
DirectLink + ConditionalLink + LoopController + back-edge through
the cycle validator's G18-Step-2 allowlist. The per-iteration LLM
+ exec mechanics are not what the cycle is proving; they're the
payload of the cycle.

An alternative design with 5 adapter Steps (envelope-to-writer-input,
writer-output-to-envelope, envelope-to-exec-input, exec-output-to-
envelope, exec-output-to-critique) would technically maximize reuse
of existing primitives but would (a) bloat the workflow YAML to
~12 step entries and ~15 link entries, (b) introduce 5 new
primitives whose only job is dict reshaping, (c) obscure the
actual iteration mechanic that this workflow is built to
demonstrate. The closed-class rule says compose existing classes
via DirectLink; this Step does compose CodeWriteStep +
IsolatedPyExecStep, just inside one Step rather than between Steps.
Step composition (Step containing Step) is a legit framework pattern;
``SubworkflowStep`` is the canonical example.

Silent-failure discipline
========================

* The inbound shape can be either (a) initial envelope from
  ``workflow_input`` or (b) the LoopController-wrapped shell from
  the back-edge. We detect via the simultaneous presence of
  ``allow_continue`` + ``payload`` keys; ambiguous shapes raise.
* Empty/missing ``code_spec`` raises (no LLM call against nothing).
* Empty/missing ``test_code`` raises (TDR's whole point is execution
  feedback — without tests, this step is just CodeWriteStep with
  a useless loop).
* The downstream ``ConditionalLink`` predicates read
  ``exec_succeeded`` from this Step's output. If the field is
  missing or non-bool, the link's predicate evaluator raises.
  We always emit ``exec_succeeded: bool`` — never None, never
  string-y "true".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps.code_write_step import CodeWriteStep
from apecx_integration.composition.steps.isolated_py_exec_step import IsolatedPyExecStep

log = logging.getLogger(__name__)

_STDERR_MAX_CHARS = 2000


class TdrIterationStepConfig(StepConfig):
    """Configuration for TdrIterationStep.

    ``extra='forbid'`` — workspace rule. YAML typos fail at config
    load rather than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    writer_config_path: str = Field(
        ...,
        description=(
            "Path to the CodeWriteStep wrapper YAML this iteration "
            "step will instantiate internally. Relative paths resolve "
            "against this YAML's directory."
        ),
    )

    executor_config_path: str = Field(
        ...,
        description=(
            "Path to the IsolatedPyExecStep wrapper YAML this iteration "
            "step will instantiate internally. Relative paths resolve "
            "against this YAML's directory."
        ),
    )

    mode: str = Field(
        default="tdr",
        description=(
            "Iteration mode. ``tdr`` (default) passes previous_attempt + "
            "critique to the writer on revision iterations — the canonical "
            "Test-Driven Refinement loop. ``best_of_n`` generates each "
            "iteration FRESH (no revision context, no critique) — same "
            "topology but the iterations are independent samples instead "
            "of refined revisions. The downstream ConditionalLink logic "
            "(short-circuit on exec_succeeded, escalate on loop_exhausted) "
            "is identical for both modes — the difference is purely in "
            "what each iteration's writer call sees as input. "
            "G104 (2026-05-17): added to support framework-native best-of-N "
            "via the same cycle topology as TDR."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    @model_validator(mode="after")
    def _validate_mode(self) -> TdrIterationStepConfig:
        if self.mode not in ("tdr", "best_of_n"):
            raise ValueError(
                f"TdrIterationStep mode must be 'tdr' or 'best_of_n', got {self.mode!r}"
            )
        return self


class TdrIterationStep(BaseStep):
    """One iteration of the TDR refine loop.

    Expected ``process()`` input shapes:

    1. **Initial** (from workflow_input via DirectLink)::

        {
            "code_spec": "Write a function that returns the n-th Fibonacci number...",
            "function_name": "fib",
            "function_signature": "def fib(n: int) -> int",  # optional
            "test_code": "assert fib(10) == 55",              # REQUIRED
            "entrypoint": "fib",                                # optional
        }

    2. **Back-edge** (from LoopController.output via ConditionalLink)::

        {
            "allow_continue": True,
            "loop_exhausted": False,
            "iteration": N,
            "max_iterations": M,
            "payload": <prior tdr_iter envelope as shown in output below>,
        }

    Output envelope (always, regardless of input shape)::

        {
            # Persistent context (forward-carried)
            "code_spec": str, "function_name": str,
            "function_signature": str | None,
            "test_code": str, "entrypoint": str | None,

            # Most recent product
            "code_source": str,

            # Exec outcome (for downstream ConditionalLink predicate)
            "stdout": str, "stderr": str, "returncode": int,
            "exec_succeeded": bool, "elapsed_seconds": float,

            # Critique for the next iteration's revision call
            "critique": str,

            # Iteration tracking (1-indexed)
            "iteration": int,
        }
    """

    COMPONENT_TYPE: str = "tdr_iteration_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name", "writer_config_path", "executor_config_path"]

    @classmethod
    def _get_config_class(cls):
        return TdrIterationStepConfig

    @classmethod
    def extract_component_config(cls, config: TdrIterationStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "writer_config_path": config.writer_config_path,
            "executor_config_path": config.executor_config_path,
            "mode": config.mode,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: TdrIterationStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)

        source_path = component_config.get("source_path")
        writer_path = self._resolve_sibling_path(
            component_config["writer_config_path"], source_path
        )
        executor_path = self._resolve_sibling_path(
            component_config["executor_config_path"], source_path
        )

        # Surface load errors at workflow-init time, not first-iteration time.
        self._writer: CodeWriteStep = CodeWriteStep.from_config(str(writer_path))
        self._executor: IsolatedPyExecStep = IsolatedPyExecStep.from_config(str(executor_path))

        # G104: ``mode`` controls whether iterations carry forward
        # previous_attempt + critique (tdr) or generate fresh samples
        # (best_of_n). Cached on the instance for the process() loop
        # to read without re-resolving the config.
        self._mode: str = component_config.get("mode", "tdr")

    @staticmethod
    def _resolve_sibling_path(configured: str, source_path: str | None) -> Path:
        p = Path(configured)
        if p.is_absolute():
            return p
        if source_path:
            return (Path(source_path).resolve().parent / p).resolve()
        return (Path.cwd() / p).resolve()

    async def process(self, input_data: Any, **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"TdrIterationStep {self.name!r}: input_data must be a dict, "
                f"got {type(input_data).__name__}"
            )

        # Framework trigger envelope unwrap (per CodeWriteStep + G99
        # LoopController precedent): when invoked through the
        # data-driven trigger cascade the step sees
        # ``{<input_data_unit_name>: <actual payload>}``. Detect + unwrap.
        # We consult ``self.step_input_data_units`` so the unwrap works
        # for ANY input data unit name (G104: needed because the
        # best_of_n_loop workflow uses ``best_of_n_iter_input`` while
        # the TDR workflow uses ``tdr_iter_input``). Direct ``.process()``
        # calls (e.g. from a test) pass the raw envelope and bypass
        # this branch.
        if (
            isinstance(input_data, dict)
            and len(input_data) == 1
            and "code_spec" not in input_data
            and "payload" not in input_data
            and "allow_continue" not in input_data
        ):
            single_key = next(iter(input_data))
            single_val = input_data[single_key]
            input_units = getattr(self, "step_input_data_units", {}) or {}
            if single_key in input_units and isinstance(single_val, dict):
                input_data = single_val

        envelope, prior_iteration = self._unwrap_envelope(input_data)

        spec = envelope.get("code_spec")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(
                f"TdrIterationStep {self.name!r}: envelope['code_spec'] must "
                f"be a non-empty string, got {type(spec).__name__}={spec!r}"
            )

        test_code = envelope.get("test_code")
        if not isinstance(test_code, str) or not test_code.strip():
            raise ValueError(
                f"TdrIterationStep {self.name!r}: envelope['test_code'] is "
                f"required and must be non-empty — TDR's value is execution "
                f"feedback; without tests this step degrades to a useless loop"
            )

        # 1. Build writer input
        writer_input: dict[str, Any] = {
            "code_spec": spec,
            "function_name": envelope.get("function_name"),
            "function_signature": envelope.get("function_signature"),
        }
        # G104: revision-mode context (previous_attempt + critique) is
        # ONLY set when mode == "tdr". In "best_of_n" mode the
        # iterations are independent samples — no context carryover.
        # The downstream cycle topology is identical either way; only
        # the writer's prompt shape differs.
        if self._mode == "tdr" and prior_iteration > 0:
            writer_input["previous_attempt"] = envelope.get("code_source")
            writer_input["critique"] = envelope.get("critique")

        # 2. Call writer
        writer_output = await self._writer.process(writer_input)
        code_source = writer_output["code_source"]

        # 3. Call executor
        exec_input = {
            "code_source": code_source,
            "test_code": test_code,
            "entrypoint": envelope.get("entrypoint"),
        }
        exec_output = await self._executor.process(exec_input)

        # 4. Format critique for next iteration (only used if exec failed
        # and the loop continues; harmless to compute either way)
        critique = self._format_critique(exec_output)

        iteration = prior_iteration + 1
        log.info(
            "TdrIterationStep %r: iteration=%d exec_succeeded=%s stderr_chars=%d",
            self.name,
            iteration,
            exec_output["exec_succeeded"],
            len(exec_output["stderr"]),
        )

        # 5. Emit envelope — always the same shape, regardless of pass/fail
        return {
            # Persistent context (for next iteration's revision)
            "code_spec": spec,
            "function_name": envelope.get("function_name"),
            "function_signature": envelope.get("function_signature"),
            "test_code": test_code,
            "entrypoint": envelope.get("entrypoint"),
            # Most recent product
            "code_source": code_source,
            # Exec outcome (downstream ConditionalLink predicates on
            # exec_succeeded; must always be a real bool, never None)
            "stdout": exec_output["stdout"],
            "stderr": exec_output["stderr"],
            "returncode": exec_output["returncode"],
            "exec_succeeded": bool(exec_output["exec_succeeded"]),
            "elapsed_seconds": exec_output["elapsed_seconds"],
            # Critique for the next iteration's revision call
            "critique": critique,
            # Tracking (1-indexed)
            "iteration": iteration,
        }

    @staticmethod
    def _unwrap_envelope(input_data: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Detect initial-envelope vs back-edge-wrapped input.

        Back-edge shape (from LoopController.output): dict with BOTH
        ``allow_continue`` AND ``payload`` keys. We unwrap and return
        the inner envelope plus the iteration count from the controller.

        Initial shape: dict without those control keys. We return as-is
        with iteration=0.

        Ambiguous shapes (some control keys present, some not) raise
        rather than silently picking a branch — operator authoring
        error, surface loudly.
        """
        has_allow = "allow_continue" in input_data
        has_payload = "payload" in input_data
        if has_allow and has_payload:
            envelope = input_data["payload"]
            if not isinstance(envelope, dict):
                raise ValueError(
                    "TdrIterationStep: loop-gate-wrapped payload must be a "
                    f"dict, got {type(envelope).__name__}"
                )
            return envelope, int(input_data.get("iteration", 0))
        if has_allow != has_payload:
            raise ValueError(
                "TdrIterationStep: ambiguous input shape — exactly one of "
                f"{{allow_continue, payload}} present. has_allow={has_allow}, "
                f"has_payload={has_payload}. Either the LoopController "
                f"emitted an unexpected shape or a workflow author wired "
                f"the wrong source."
            )
        return input_data, 0

    @staticmethod
    def _format_critique(exec_output: dict[str, Any]) -> str:
        """Turn an exec result into a critique string for the writer's
        revision call. The format matches the Python TDR's existing
        format so the parity test stays meaningful.
        """
        if exec_output["exec_succeeded"]:
            return ""  # No critique on pass; not used by next iteration
        stderr = (exec_output.get("stderr") or "").strip()
        if not stderr:
            stderr = (
                f"Subprocess returned exit code {exec_output.get('returncode')} "
                "with no stderr output."
            )
        if len(stderr) > _STDERR_MAX_CHARS:
            stderr = stderr[-_STDERR_MAX_CHARS:]
        return (
            "The previous attempt failed when run against the test code. "
            "Below is the stderr from the test execution (truncated to the "
            "last 2000 chars). Read the failure carefully, identify the "
            "specific defect, and write a corrected version.\n\n"
            f"=== stderr ===\n{stderr}"
        )
