"""TX5 AC3 fixture: an incompliant step that overrides ``execute``.

``scripts/checks/step_authoring.py`` should reject this file.
"""

from __future__ import annotations

from typing import Any


class BaseStep:
    """Stand-in so the fixture parses without a nanobrain dependency."""


class BadStep(BaseStep):
    async def execute(self, input_data: dict[str, Any]) -> Any:
        # WRONG — execute is framework-owned infrastructure; put business
        # logic in process() instead. This is the FAIL-FAST check
        # described in nanobrain-step-authoring skill.
        return {"overridden": True}
