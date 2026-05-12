"""CodeReflectionStep — generate→critique reasoning pattern as a step.

A thin concrete subclass of nanobrain's :class:`SubworkflowStep` that
embeds the bundled ``code_reflection_workflow.yml`` (CodeWriteStep
followed by CodeReviewStep) and surfaces it as an ordinary step
class. From the composer's POV the workflow-embedding mechanism is
invisible — the composer just sees ``CodeReflectionStep`` like
``RagSynthesisStep`` or any other production step, picks it via
RAG match against the wrapper YAML's ``rag_description``, and wires
it into the generated workflow.

Why this shape (vs. exposing SubworkflowStep with an
``inner_workflow_path`` field): handing the LLM a "workflow path"
field is a fresh hallucination surface — the LLM will invent paths
that look plausible but don't exist. A concrete subclass hardcodes
the path; the composer never authors it.

Reusable across domains? **Not yet, honestly.** This wrapper is
code-specific (the inner workflow names CodeWriteStep and
CodeReviewStep concretely). The cross-domain pattern — where the
generator and critic are typed bindings filled by the composer —
ships via G9 ``Workflow.from_skeleton`` with ``{{generator: Step}}``
placeholders. See ``CATALOG.md`` for the documented recipe.
"""

from __future__ import annotations

from typing import Any

from nanobrain.library.steps.subworkflow_step import SubworkflowStep


class CodeReflectionStep(SubworkflowStep):
    """Embed the code-reflection sub-workflow (write → review) as a step.

    Expected ``process()`` input::

        {
            "code_spec": "Write a fibonacci function...",
            "function_name": "fib",                   # required by code_write_step
            "function_signature": "def fib(n: int) -> int",  # optional
            "previous_attempt": "...",                # optional, for refinement
            "critique": "...",                        # optional, for refinement
        }

    Return shape (collected from the inner workflow's output data units)::

        {
            "code_source": "def fib(n: int) -> int: ...",
            "function_name_verified": "fib",
            "review_verdict": {
                "approved": True,
                "reasoning": "...",
                "concerns": [...],
                "suggestions": [...],
                "raw_response": "..."
            },
        }

    Raises:
        Whatever the inner code_write or code_review steps raise, via
        the SubworkflowStep's status-check + EMPTY-OUTPUT gates.
    """

    COMPONENT_TYPE: str = "code_reflection_step"

    @classmethod
    def _default_inner_workflow_path(cls) -> str | None:
        # Resolved by SubworkflowStep against the workspace root via
        # G40's locate_workflow_root, OR cwd as fallback.
        return (
            "src/apecx_integration/composition/workflows/code_writing/code_reflection_workflow.yml"
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Run the embedded write → review sub-workflow.

        Explicit override delegates to ``SubworkflowStep.process`` so
        the subclass surface declares the contract: input keys are
        ``code_spec`` (required), ``function_name``,
        ``function_signature``, ``previous_attempt``, ``critique``;
        output keys are ``code_source``, ``function_name_verified``,
        and ``review_verdict``.
        """
        return await super().process(input_data, **kwargs)


__all__ = ["CodeReflectionStep"]
