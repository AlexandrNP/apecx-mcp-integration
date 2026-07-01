"""#3 (2026-07-01) — LocalExecutor names the in-flight / failed step in its failure reason.

When ``Workflow.run`` returns ``status='cascade_timeout'`` (or a step raises), the executor's
failure reason used to carry only "Partial output keys: [...]" — never WHICH step hung or raised.
The executor now captures G37 step events during the run and ``_format_step_diagnostics()`` turns
them into a suffix naming the in-flight and/or failed step. This pins that formatter; the
event-capture that feeds it mirrors the proven ``cli/_globus_data_transfer.py`` pattern.
"""

from __future__ import annotations

from apecx_integration.control_plane.executors.local import _format_step_diagnostics


def test_names_in_flight_step():
    # A hung step: started but never done.
    out = _format_step_diagnostics(started=["fetch", "align"], done={"fetch"}, failures=[])
    assert "in-flight step(s): ['align']" in out
    assert out.endswith(".")


def test_names_failed_step_with_exception():
    out = _format_step_diagnostics(
        started=["fetch"], done={"fetch"}, failures=[("fetch", "ValueError", "no sequences")]
    )
    assert "fetch raised ValueError: no sequences" in out


def test_names_both_in_flight_and_failed():
    out = _format_step_diagnostics(
        started=["a", "b", "c"], done={"a", "b"}, failures=[("b", "RuntimeError", "boom")]
    )
    assert "in-flight step(s): ['c']" in out
    assert "b raised RuntimeError: boom" in out


def test_empty_when_nothing_captured():
    assert _format_step_diagnostics(started=[], done=set(), failures=[]) == ""
    # All started steps completed -> no in-flight, no failures -> empty (no spurious suffix).
    assert _format_step_diagnostics(started=["a"], done={"a"}, failures=[]) == ""
