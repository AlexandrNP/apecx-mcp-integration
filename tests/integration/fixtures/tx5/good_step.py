"""TX5 AC3 fixture: a compliant step subclass.

Implements ``async def process``, does NOT override ``execute``.
``scripts/checks/step_authoring.py`` should accept this file.
"""

from __future__ import annotations

from typing import Any


class BaseStep:
    """Stand-in so the fixture parses without a nanobrain dependency."""


class CompliantStep(BaseStep):
    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        return {"ok": True, "echo": input_data}
