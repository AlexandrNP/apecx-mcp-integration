"""CodeWithTestsStep — generate-code-AND-tests reasoning pattern.

Concrete subclass of :class:`SubworkflowStep` that embeds
``code_with_tests_workflow.yml`` (CodeWriteStep → TestWriteStep)
and surfaces it to the composer as an ordinary step class.

Pairs nicely with ``CodeVerificationStep``: the output of
``CodeWithTestsStep`` is exactly the input shape that
``IsolatedPyExecStep`` consumes — ``{code_source, test_code,
function_name?}``. So an outer workflow can compose:

    CodeWithTestsStep → CodeVerificationStep

to get a "write + tests + run-tests" pipeline.
"""

from __future__ import annotations

from typing import Any

from nanobrain.library.steps.subworkflow_step import SubworkflowStep


class CodeWithTestsStep(SubworkflowStep):
    """Embed the code-with-tests sub-workflow as a step.

    Expected ``process()`` input::

        {
            "code_spec": "Write a function ...",
            "function_name": "fib",                   # required
            "function_signature": "def fib(n: int) -> int",  # optional
        }

    Return shape::

        {
            "test_code": "def test_fib(): ...",
            "test_function_count": 3,
            "code_source": "def fib(n: int) -> int: ...",
            ...   # other passthrough fields
        }
    """

    COMPONENT_TYPE: str = "code_with_tests_step"

    @classmethod
    def _default_inner_workflow_path(cls) -> str | None:
        return (
            "src/apecx_integration/composition/workflows/code_writing/code_with_tests_workflow.yml"
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Run the embedded code → tests sub-workflow.

        Explicit override delegates to ``SubworkflowStep.process`` so
        the subclass surface declares the contract.
        """
        return await super().process(input_data, **kwargs)


__all__ = ["CodeWithTestsStep"]
