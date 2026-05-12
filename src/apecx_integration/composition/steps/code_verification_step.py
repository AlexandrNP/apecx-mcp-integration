"""CodeVerificationStep — runtime verification reasoning pattern as a step.

Concrete subclass of nanobrain's :class:`SubworkflowStep` that embeds
the bundled ``code_verification_workflow.yml`` (a single
``IsolatedPyExecStep``). Surfaces runtime verification to the
composer as an ordinary step.

REFUSES TO EXECUTE unless ``APECX_CODE_EXEC=1`` is set in the
operator environment — the gate fires inside the inner
``IsolatedPyExecStep``. The wrapper inherits that posture without
modification; nothing the composer authors at compose time can
flip it.
"""

from __future__ import annotations

from typing import Any

from nanobrain.library.steps.subworkflow_step import SubworkflowStep


class CodeVerificationStep(SubworkflowStep):
    """Embed the code-verification sub-workflow as a step.

    Expected ``process()`` input::

        {
            "code_source": "def fib(n): ...",
            "test_code": "assert fib(10) == 55",   # optional
            "entrypoint": "fib",                     # optional
        }

    Return shape::

        {
            "exec_result": {
                "stdout": "...",
                "stderr": "...",
                "returncode": 0,
                "exec_succeeded": True,
                "elapsed_seconds": 0.42,
            },
        }

    The result dict is structured rather than raising on failure so
    a surrounding workflow can decide whether to retry, refine, or
    escalate based on ``exec_succeeded``.
    """

    COMPONENT_TYPE: str = "code_verification_step"

    @classmethod
    def _default_inner_workflow_path(cls) -> str | None:
        return (
            "src/apecx_integration/composition/workflows/code_writing/"
            "code_verification_workflow.yml"
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Run the embedded isolated-exec sub-workflow.

        Explicit override delegates to ``SubworkflowStep.process`` so
        the subclass surface declares the contract: input keys are
        ``code_source`` (required), ``test_code``, ``entrypoint``;
        output key is ``exec_result`` (a dict with stdout, stderr,
        returncode, exec_succeeded, elapsed_seconds).
        """
        return await super().process(input_data, **kwargs)


__all__ = ["CodeVerificationStep"]
