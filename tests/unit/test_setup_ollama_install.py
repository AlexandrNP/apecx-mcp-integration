"""Pin the apecx-setup Ollama install/check integration (2026-05-11).

Pre-2026-05-11, ``apecx-setup llm`` only CHECKED for ``ollama`` in
PATH + the daemon's reachability + the model's presence. When the
CLI was missing it printed a pointer to ``https://ollama.com/download``
and SKIPPED. The fresh-laptop friction this caused (read the README,
get to step 3, hit a SKIP, manually install Ollama, re-run) was real
enough to justify integrating the install into the orchestrator.

Contracts pinned:

  * ``_offer_install_ollama(interactive=False)`` MUST return False
    when ``ollama`` is not on PATH. Non-interactive mode never runs
    ``brew install`` / ``curl | sh`` on its own — that's a CI /
    automation safety property.
  * ``_offer_install_ollama(interactive=True)`` returns True when
    ``ollama`` is already on PATH (no-op fast path).
  * ``_offer_start_ollama_daemon(interactive=False)`` returns True
    when the daemon is already reachable; False when not (no
    background spawn attempted in non-interactive mode).
  * ``_step_llm(interactive=False)`` SKIPS gracefully when CLI is
    missing — matches the pre-2026-05-11 behavior so CI runs do
    not unexpectedly start invoking installers.

These tests do NOT exercise the brew / curl|sh install path itself
(that would require either mocking shutil + subprocess heavily, or
running the actual installer — both bad). The install path is
tested in interactive smoke runs on a fresh laptop; here we pin
only the safety properties.
"""

from __future__ import annotations

from unittest import mock

from apecx_integration.cli import setup


def test_offer_install_ollama_no_op_when_already_installed():
    """If ``ollama`` is on PATH, no prompt is issued; returns True."""
    with mock.patch.object(setup.shutil, "which", return_value="/usr/local/bin/ollama"):
        assert setup._offer_install_ollama(interactive=True) is True
        assert setup._offer_install_ollama(interactive=False) is True


def test_offer_install_ollama_skips_when_non_interactive_and_missing():
    """Non-interactive mode MUST NOT invoke any installer when ollama
    is missing. The safety property guards CI / scripted runs."""
    with (
        mock.patch.object(setup.shutil, "which", return_value=None),
        mock.patch.object(setup.subprocess, "run") as run_mock,
    ):
        assert setup._offer_install_ollama(interactive=False) is False
        run_mock.assert_not_called()


def test_offer_start_daemon_no_op_when_reachable():
    """If the daemon already responds, no spawn happens."""
    with (
        mock.patch.object(setup, "_ollama_daemon_reachable", return_value=True),
        mock.patch.object(setup.subprocess, "Popen") as popen_mock,
    ):
        assert setup._offer_start_ollama_daemon(interactive=True) is True
        assert setup._offer_start_ollama_daemon(interactive=False) is True
        popen_mock.assert_not_called()


def test_offer_start_daemon_skips_when_non_interactive_and_unreachable():
    """Non-interactive mode MUST NOT spawn ``ollama serve`` when
    the daemon is unreachable. CI runs install + start out-of-band."""
    with (
        mock.patch.object(setup, "_ollama_daemon_reachable", return_value=False),
        mock.patch.object(setup.subprocess, "Popen") as popen_mock,
    ):
        assert setup._offer_start_ollama_daemon(interactive=False) is False
        popen_mock.assert_not_called()


def test_step_llm_skips_gracefully_when_nothing_serving_and_no_docker():
    """The load-bearing CI-safety property: ``_step_llm`` returns a SKIP status (not a FAIL, not an
    exception) when it cannot provision Ollama — nothing serving AND no docker to start the
    apecx-ollama container (#7 container-first flow; no host `ollama` binary is consulted)."""
    with (
        mock.patch.dict(setup.os.environ, {"APECX_LLM_BASE_URL": ""}),
        mock.patch.object(setup, "_ollama_reachable", return_value=False),
        mock.patch.object(setup, "_docker_available", return_value=False),
        mock.patch.object(setup, "_offer_install_ollama", return_value=False),  # host fallback n/a
    ):
        result = setup._step_llm(interactive=False)
    assert result.status == "skipped"
    assert "docker" in result.detail.lower()


def test_step_llm_interactive_default_attribute_present():
    """Signature regression guard: _step_llm must accept the
    ``interactive`` kwarg so _run_all + the subcommand dispatcher
    can pass it through. Pre-2026-05-11 _step_llm took no args and
    the install integration could not be wired."""
    import inspect

    sig = inspect.signature(setup._step_llm)
    assert "interactive" in sig.parameters
    assert sig.parameters["interactive"].default is True
