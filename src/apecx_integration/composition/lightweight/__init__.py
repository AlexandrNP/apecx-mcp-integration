"""Programmatic (lightweight) variants of the code-writing workflows.

When to use this vs. the YAML form:

  * **YAML** (workflows/code_writing/*.yml): the canonical, declarative
    representation. Read by the composer + MCP discovery; what
    operators inspect, audit, diff. Use this for any workflow that
    will be shipped, reviewed, or executed in production.

  * **Programmatic** (this module): builds the same Workflow via the
    nanobrain.lightweight.WorkflowBuilder API. Use this when:
      - An agent or another piece of Python code is constructing
        the workflow on the fly (e.g., the composer might one day
        emit Python instead of YAML).
      - You want to inject runtime-resolved parameters that are
        awkward in YAML (a callable, a live database handle).
      - You're writing a test fixture that needs to vary topology
        per test without staging YAML files.

Both paths produce a real ``Workflow`` instance via
``Workflow.from_config`` under the hood — same v2 validation, same
auto_transfer=true mutator firing, same Pydantic gate. The
programmatic path is NOT a separate framework; it's a sugar layer
over YAML construction.
"""

from .code_reflection_lightweight import (
    build_code_reflection_workflow,
)

__all__ = ["build_code_reflection_workflow"]
