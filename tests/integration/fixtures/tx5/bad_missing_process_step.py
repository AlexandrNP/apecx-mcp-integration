"""TX5 AC3 fixture: step subclass missing ``process``.

``scripts/checks/step_authoring.py`` should reject this file.
"""

from __future__ import annotations


class BaseStep:
    """Stand-in so the fixture parses without a nanobrain dependency."""


class IncompleteStep(BaseStep):
    # No process() method. Step must implement process; framework will
    # raise ComponentConfigurationError at init without it, but this
    # check catches it at commit time.
    COMPONENT_TYPE = "incomplete_step"
