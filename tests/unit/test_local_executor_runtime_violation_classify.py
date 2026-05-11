"""C2 — unit tests for ``LocalExecutor._classify_runtime_violation``.

Pure-function classification: each framework error message must map
onto a stable rule_id mirroring the A1 validator vocabulary.

A future framework rewording that silently invalidates a marker
would flatline the C2 regression metric — these tests are the
canary.
"""

from __future__ import annotations

from apecx_integration.control_plane.executors.local import LocalExecutor


def _classify(message: str, exc_type: type[Exception] = ValueError) -> str:
    """Build an exception with the given message + delegate.

    ``_classify_runtime_violation`` is a classmethod, so we call it
    on the class directly — no instance setup needed.
    """
    return LocalExecutor._classify_runtime_violation(exc_type(message))


def test_classifies_verbatim_inline_dict_violation():
    msg = (
        "❌ FRAMEWORK VIOLATION: Inline dict configuration not "
        "supported for foo.bar.Baz\n"
        "   SUPPORTED CLASSES: DataUnit, Link, Trigger and their "
        "subclasses only\n"
        "   REQUIRED: Use file path for config field\n"
        "   EXAMPLE: config: 'path/to/baz.yml'\n"
        "   CURRENT: config: {a: 1}"
    )
    assert _classify(msg) == "step_inline_config_forbidden"


def test_classifies_generic_framework_violation():
    """A framework violation we DON'T have a specific marker for
    falls into the unclassified bucket so it's still surfaced as
    a framework concern (not lumped under runtime_other)."""
    msg = "❌ FRAMEWORK VIOLATION: some new rule we haven't seen before"
    assert _classify(msg) == "framework_violation_unclassified"


def test_classifies_failed_to_instantiate():
    msg = "❌ FAILED TO INSTANTIATE OBJECT: foo"
    assert _classify(msg) == "from_config_failed"


def test_classifies_module_not_found():
    assert (
        _classify("No module named 'foo.bar'", exc_type=ModuleNotFoundError) == "module_not_found"
    )


def test_classifies_attribute_error():
    assert _classify("module has no attribute Foo", exc_type=AttributeError) == "attribute_error"


def test_unknown_message_falls_through_to_runtime_other():
    """Anything we can't classify gets the catch-all so the metric
    still counts the occurrence — operators can then inspect the
    message and decide if a new marker is warranted."""
    assert _classify("something completely unfamiliar") == "runtime_other"


def test_inline_dict_marker_wins_over_generic_framework_violation():
    """The most-specific marker must win — ordering of markers
    matters for the rule_id distribution. If the generic marker
    started firing on inline-dict cases, the metric for the
    specific failure shape would be lost in the noise."""
    msg = "❌ FRAMEWORK VIOLATION: Inline dict configuration not supported for X"
    assert _classify(msg) == "step_inline_config_forbidden"
