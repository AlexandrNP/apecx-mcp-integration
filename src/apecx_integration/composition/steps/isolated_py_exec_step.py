"""IsolatedPyExecStep — run Python source in an isolated subprocess.

**NOT a security sandbox.**

This step isolates code execution from the parent process so an
accidental ``os.environ.clear()`` or stray module-level side effect
in LLM-authored code does not corrupt the workflow runtime. It does
NOT defend against malicious code. Anyone running this step on code
they do not control should be using a real sandbox
(``apecx_integration.composition.docker_sandbox.DockerSandboxRunner``,
gated by ``APECX_T13B_SANDBOX_EXECUTE=1`` — itself a higher-trust
posture than this step).

Threat model in plain terms:

* **In scope**: accidental import-time side effects, stray writes
  to cwd, stdout pollution, infinite loops (the timeout kills
  them), modules that mutate ``sys.path`` at import.
* **Out of scope**: code that opens sockets, exfiltrates env vars,
  drops shells, reads ``~/.ssh/``, or otherwise behaves adversarially.

Refuse-by-default posture (mirrors T13b's
``APECX_T13B_SANDBOX_EXECUTE=1``):

The step REFUSES to execute unless ``APECX_CODE_EXEC=1`` is set in
the environment, even when the wrapper YAML configures it. The env
gate is operator-controlled; the step config is workflow-author-
controlled; we trust the operator more than the workflow author
(who in our setting is often an LLM via the composer).

What you get back per ``process()`` call:

  ``{"stdout": str, "stderr": str, "returncode": int,
     "exec_succeeded": bool, "elapsed_seconds": float}``

``exec_succeeded`` is True iff ``returncode == 0``. The step does
NOT raise on returncode != 0 — downstream review/retry logic needs
the structured failure data to act on. The step DOES raise on:

* empty / non-string ``code_source`` (EMPTY-FAIL discipline);
* unparseable ``code_source`` (no point spawning a subprocess);
* missing env opt-in.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import subprocess
import sys
import time
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


_ENV_GATE = "APECX_CODE_EXEC"


# Minimal env passed to the subprocess. We deliberately do NOT
# inherit os.environ — APECX_LLM_API_KEY, GITHUB_TOKEN, etc. should
# not be visible to LLM-authored code. Operators who need a specific
# variable (e.g. PATH for finding the python interpreter) opt in
# explicitly via the step config's `extra_env`.
_BASE_SUBPROCESS_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PATH": "/usr/bin:/bin",
}


class IsolatedPyExecStepConfig(StepConfig):
    """Configuration for IsolatedPyExecStep.

    ``extra='forbid'`` (workspace rule).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)
    source_path: str | None = Field(default=None)

    timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        le=300.0,
        description=(
            "Hard wall-clock limit per process() call. The subprocess "
            "is killed when it exceeds this. Defaults to 5s — long "
            "enough for simple algorithm code, short enough to catch "
            "infinite loops fast. Range is bounded at 300s to prevent "
            "an operator footgun (a 1-hour step blocking the workflow)."
        ),
    )

    python_executable: str | None = Field(
        default=None,
        description=(
            "Optional override for the Python interpreter. Defaults "
            "to the same one running the workflow (sys.executable) "
            "for site-package compatibility. Set this only when "
            "running tests under a specific Python version."
        ),
    )

    extra_env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Additional environment variables to pass to the "
            "subprocess. Merged AFTER the base scrubbed env, so "
            "operators can re-introduce variables they need (e.g. "
            "TMPDIR) without inheriting the parent's full env."
        ),
    )

    cwd: str | None = Field(
        default=None,
        description=(
            "Working directory for the subprocess. Defaults to a "
            "newly-created temp directory per call (provides clean "
            "isolation against incidental file writes). Set this "
            "only when the executed code legitimately needs to read "
            "or write a known path."
        ),
    )

    raise_on_nonzero_returncode: bool = Field(
        default=False,
        description=(
            "When False (default), a non-zero returncode is reported "
            "via the result dict (exec_succeeded=False) so downstream "
            "review/retry steps can act on it. When True, raise a "
            "RuntimeError instead — use this when the step is the "
            "final gate in a workflow that has no recovery path."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class IsolatedPyExecStep(BaseStep):
    """Run Python source in a subprocess; capture stdout/stderr/returncode.

    Expected ``process()`` input::

        {
            "code_source": "def fib(n): ...",
            "test_code": "assert fib(10) == 55",   # optional, appended
            "entrypoint": "fib",                     # optional, ignored unless test_code is empty
        }

    When ``test_code`` is supplied, it is appended after ``code_source``
    with a newline so the function definitions + the assertion code
    run as a single script. When neither is supplied and
    ``entrypoint`` is set, we append ``print(<entrypoint>())`` so the
    function is at least invoked. When all three are absent, the code
    is run as-is (useful for module-level smoke tests).

    The result dict shape is documented in the module docstring.
    """

    COMPONENT_TYPE: str = "isolated_py_exec_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return IsolatedPyExecStepConfig

    @classmethod
    def extract_component_config(cls, config: IsolatedPyExecStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "timeout_seconds": config.timeout_seconds,
            "python_executable": config.python_executable,
            "extra_env": dict(config.extra_env or {}),
            "cwd": config.cwd,
            "raise_on_nonzero_returncode": config.raise_on_nonzero_returncode,
        }

    def _init_from_config(
        self,
        config: IsolatedPyExecStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._timeout_seconds: float = float(component_config["timeout_seconds"])
        self._python_executable: str = component_config.get("python_executable") or sys.executable
        self._extra_env: dict[str, str] = dict(component_config["extra_env"])
        self._cwd: str | None = component_config.get("cwd")
        self._raise_on_nonzero: bool = bool(component_config["raise_on_nonzero_returncode"])

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if os.environ.get(_ENV_GATE) != "1":
            raise RuntimeError(
                f"IsolatedPyExecStep {self.name!r}: refused to execute. "
                f"Set ${_ENV_GATE}=1 in the operator environment to "
                f"enable. This step runs LLM-authored code in a "
                f"subprocess; it is NOT a security sandbox. Operators "
                f"opt in explicitly so a workflow YAML alone cannot "
                f"trigger execution."
            )

        if not isinstance(input_data, dict):
            raise ValueError(
                f"IsolatedPyExecStep {self.name!r}: input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )

        if (
            "exec_input" in input_data
            and isinstance(input_data["exec_input"], dict)
            and "code_source" not in input_data
        ):
            input_data = input_data["exec_input"]

        code = input_data.get("code_source")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(
                f"IsolatedPyExecStep {self.name!r}: "
                f"input_data['code_source'] must be a non-empty string, "
                f"got {type(code).__name__}={code!r}"
            )

        # AST gate — no point spawning a subprocess for unparseable
        # code. The subprocess would raise SyntaxError too, but the
        # AST check is faster and the error message is more useful.
        try:
            ast.parse(code)
        except SyntaxError as e:
            raise ValueError(
                f"IsolatedPyExecStep {self.name!r}: code_source is not "
                f"valid Python (line {e.lineno}: {e.msg!r}). Refusing "
                f"to spawn subprocess."
            ) from e

        script = self._compose_script(
            code=code,
            test_code=input_data.get("test_code"),
            entrypoint=input_data.get("entrypoint"),
        )

        env = dict(_BASE_SUBPROCESS_ENV)
        env.update(self._extra_env)

        result = await asyncio.to_thread(self._run_subprocess, script=script, env=env)

        log.info(
            "IsolatedPyExecStep %r: returncode=%d, elapsed=%.3fs, stdout=%d chars, stderr=%d chars",
            self.name,
            result["returncode"],
            result["elapsed_seconds"],
            len(result["stdout"]),
            len(result["stderr"]),
        )

        if self._raise_on_nonzero and not result["exec_succeeded"]:
            raise RuntimeError(
                f"IsolatedPyExecStep {self.name!r}: subprocess returned "
                f"non-zero exit code {result['returncode']}. "
                f"stderr (last 500 chars): {result['stderr'][-500:]!r}"
            )

        return result

    @staticmethod
    def _compose_script(*, code: str, test_code: str | None, entrypoint: str | None) -> str:
        body = code.rstrip()
        if test_code and test_code.strip():
            body += "\n\n" + test_code.strip() + "\n"
        elif entrypoint:
            # Defensive: only append the invocation if the entrypoint
            # is a valid identifier — otherwise we'd inject arbitrary
            # text into the script.
            if not entrypoint.isidentifier():
                raise ValueError(
                    f"IsolatedPyExecStep: entrypoint {entrypoint!r} is "
                    f"not a valid Python identifier"
                )
            body += f"\n\nprint({entrypoint}())\n"
        else:
            body += "\n"
        return body

    def _run_subprocess(self, *, script: str, env: dict[str, str]) -> dict[str, Any]:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [self._python_executable, "-I", "-c", script],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                env=env,
                cwd=self._cwd,
                check=False,
            )
            elapsed = time.monotonic() - start
            return {
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "returncode": proc.returncode,
                "exec_succeeded": proc.returncode == 0,
                "elapsed_seconds": elapsed,
            }
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - start
            return {
                "stdout": (e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")),
                "stderr": (
                    (e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""))
                    + f"\n[IsolatedPyExecStep timeout: {self._timeout_seconds}s]\n"
                ),
                "returncode": -1,
                "exec_succeeded": False,
                "elapsed_seconds": elapsed,
            }


__all__ = ["IsolatedPyExecStep", "IsolatedPyExecStepConfig"]
