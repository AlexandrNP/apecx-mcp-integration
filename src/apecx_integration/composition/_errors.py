"""Composer error hierarchy.

Lives in a small module to break circular imports: ``composer.py`` and
``workflow_validator.py`` both need the same base class, but
``workflow_validator`` would otherwise import the composer (which
imports the validator). Factor the shared symbols here.

Public re-exports remain on ``apecx_integration.composition.composer``
for backward compat — existing callers do
``from apecx_integration.composition.composer import ComposerResponseError``.
"""

from __future__ import annotations


class ComposerConfigurationError(ValueError):
    """Raised when a composer config is structurally wrong."""


class ComposerResponseError(ValueError):
    """Raised when the LLM response can't be parsed into a workflow.

    Separate from ``ComposerConfigurationError`` so callers can
    distinguish "operator misconfigured the composer" from "LLM
    emitted unparseable output."
    """


__all__ = [
    "ComposerConfigurationError",
    "ComposerResponseError",
]
