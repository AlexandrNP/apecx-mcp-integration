"""T14 mocks-policy enforcement — tests for the nanobrain fixes.

Per the T14 audit (``docs/nanobrain_mock_audit.md``) and its approved
§4 carve-out #2 fixes, this file verifies the new **error paths** that
replaced the previous silent mock fallbacks in nanobrain.

The actual nanobrain edits live at:
- ``nanobrain/core/a2a_support.py`` (row 1)
- ``nanobrain/core/academy_integration.py`` (row 2)
- ``nanobrain/config/global_config.yml`` + ``nanobrain/core/config/config_manager.py`` (rows 3+4)
- ``nanobrain/library/tools/bioinformatics/pubmed_client.py`` (row 5)

nanobrain is not a git repo on this workspace (friction log #6), so
these tests are the durable artifact of the fixes — if the nanobrain
edits get reverted (e.g., by a re-fetch), these tests flip red and
the regression is visible.

A2A happy-path parity (T-2026-04-23-01): the error paths in row 1
below have a matching positive-path integration test at
``tests/integration/test_a2a_happy_path.py`` which exercises the
full ``connect → discover → send → get → cancel`` lifecycle against
a real in-process aiohttp JSON-RPC server (no mocks).

Academy happy-path parity (T-2026-04-23-02, G5, 2026-04-24): the
error paths in row 2 below have a matching positive-path integration
test at ``tests/integration/test_academy_real_integration.py`` which
launches a real ``academy.agent.Agent`` via the nanobrain manager
wrapper and dispatches actions through the real
``academy.handle.Handle`` — verifying both the full lifecycle AND
that ``ACADEMY_DEMO_MODE=1`` still produces the aurora-demo mock.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Row 1 — a2a_support.py
# ---------------------------------------------------------------------------

def test_a2a_not_available_error_class_exists():
    """Pre-T14 there was no A2ANotAvailableError — callers had no way
    to distinguish "aiohttp missing" from other failures because the
    code silently returned mocks instead."""
    from nanobrain.core.a2a_support import A2AError, A2ANotAvailableError

    assert issubclass(A2ANotAvailableError, A2AError)


def test_a2a_connect_raises_when_aiohttp_missing(monkeypatch):
    """Simulate ``AIOHTTP_AVAILABLE=False`` and verify ``connect_to_agent``
    raises instead of silently installing a ``MockA2ASession``.
    """
    from nanobrain.core import a2a_support
    from nanobrain.core.a2a_support import (
        A2AAgentConfig,
        A2AClient,
        A2AConnectionError,
    )

    monkeypatch.setattr(a2a_support, "AIOHTTP_AVAILABLE", False)

    client = A2AClient()
    client.add_agent(A2AAgentConfig(
        name="test-agent",
        url="http://example.invalid",
        description="test",
    ))

    # Behavior: raises A2AConnectionError wrapping A2ANotAvailableError,
    # per the except-block wrap in connect_to_agent.
    with pytest.raises(A2AConnectionError, match="aiohttp is not installed"):
        asyncio.run(client.connect_to_agent("test-agent"))


def test_a2a_send_task_raises_via_connect_when_aiohttp_missing(monkeypatch):
    """If ``AIOHTTP_AVAILABLE=False`` and no session was pre-populated,
    ``send_task`` calls ``connect_to_agent`` at its top, which now
    raises ``A2AConnectionError`` (wrapping ``A2ANotAvailableError``).

    NOTE: send_task's OWN else-branch (which I also patched to raise
    A2ANotAvailableError) is actually unreachable in this flow —
    connect fails first. The defensive raise is kept for the case
    where a session has been cleared between calls; not worth
    instrumenting a test for.
    """
    from nanobrain.core import a2a_support
    from nanobrain.core.a2a_support import (
        A2AAgentConfig,
        A2AClient,
        A2AConnectionError,
        A2AMessage,
    )

    monkeypatch.setattr(a2a_support, "AIOHTTP_AVAILABLE", False)

    client = A2AClient()
    client.add_agent(A2AAgentConfig(
        name="test-agent",
        url="http://example.invalid",
        description="test",
    ))

    with pytest.raises(A2AConnectionError, match="aiohttp is not installed"):
        asyncio.run(client.send_task(
            "test-agent",
            task_id="t-123",
            message=A2AMessage(role="user", parts=[]),
        ))


# ---------------------------------------------------------------------------
# Row 2 — academy_integration.py
# ---------------------------------------------------------------------------

def test_academy_not_implemented_error_class_exists():
    from nanobrain.core.academy_integration import AcademyNotImplementedError

    assert issubclass(AcademyNotImplementedError, NotImplementedError)


# Removed 2026-04-24 (audit §4.2):
# ``test_academy_demo_mode_env_var_gate_documented`` was a source-
# string grep against ``academy_integration.py`` checking that the
# strings ``ACADEMY_DEMO_MODE`` and ``AcademyNotImplementedError``
# appeared in the source. That test would have passed if those
# names appeared in a comment or a removed-but-imported symbol —
# i.e., it tested documentation, not behavior.
#
# Behavioral coverage of the demo-mode gate is now provided by
# ``tests/integration/test_academy_real_integration.py::
# test_demo_mode_still_produces_mock_response`` (added with the G5
# real-Academy integration, 2026-04-24). That test sets
# ``ACADEMY_DEMO_MODE=1``, dispatches a real action, and asserts
# the mock response shape — a real "remove the gate" commit would
# fail that test, not just trigger a string mismatch in a comment.


# ---------------------------------------------------------------------------
# Rows 3+4 — config_manager.py use_mock_clients default + warning
# ---------------------------------------------------------------------------

def test_use_mock_clients_default_is_false(tmp_path, monkeypatch):
    """When a config has no ``development`` block at all, is_development_mode
    must return False. Pre-T14 the default-config dict shipped with
    use_mock_clients=True, which we flipped.
    """
    from nanobrain.core.config.config_manager import ConfigManager

    cfg = tmp_path / "empty_config.yml"
    cfg.write_text("framework:\n  name: test\n")

    mgr = ConfigManager(str(cfg))
    mgr.load_config()

    assert mgr.is_development_mode() is False


def test_use_mock_clients_true_emits_warning(tmp_path, caplog):
    """When a config explicitly sets use_mock_clients=True, the framework
    emits a ⚠️  warning on the first is_development_mode() call so
    operators can't miss that they're running with mock clients."""
    import logging

    from nanobrain.core.config.config_manager import ConfigManager

    cfg = tmp_path / "devmode_config.yml"
    cfg.write_text(
        "framework:\n  name: test\n"
        "development:\n  use_mock_clients: true\n"
    )

    mgr = ConfigManager(str(cfg))
    mgr.load_config()

    with caplog.at_level(logging.WARNING, logger="nanobrain.core.config.config_manager"):
        assert mgr.is_development_mode() is True

    warning_texts = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert any("dev-mode" in m and "mock" in m.lower() for m in warning_texts), (
        f"Expected dev-mode warning; got warnings: {warning_texts!r}"
    )


def test_use_mock_clients_warning_emits_once(tmp_path, caplog):
    """Log-spam guard: the warning fires once per ConfigManager instance,
    not on every call."""
    import logging

    from nanobrain.core.config.config_manager import ConfigManager

    cfg = tmp_path / "devmode_config.yml"
    cfg.write_text(
        "framework:\n  name: test\n"
        "development:\n  use_mock_clients: true\n"
    )

    mgr = ConfigManager(str(cfg))
    mgr.load_config()

    with caplog.at_level(logging.WARNING, logger="nanobrain.core.config.config_manager"):
        mgr.is_development_mode()
        mgr.is_development_mode()
        mgr.is_development_mode()

    warning_count = sum(
        1 for r in caplog.records
        if r.levelno >= logging.WARNING and "dev-mode" in r.message
    )
    assert warning_count == 1, f"Expected 1 warning, got {warning_count}"


# ---------------------------------------------------------------------------
# Row 5 — pubmed_client.py
# ---------------------------------------------------------------------------

def test_pubmed_search_raises_not_implemented():
    """Pre-T14: returned ``[]`` silently and cached it. Post-T14: raises
    NotImplementedError pointing at Phase 4B.
    """
    from nanobrain.library.tools.bioinformatics.pubmed_client import PubMedClient

    # Construct against a default PubMedConfig. The real NCBI config
    # doesn't matter — the method raises before touching anything.
    client = PubMedClient.__new__(PubMedClient)
    client.logger = __import__('logging').getLogger("test")
    client.pubmed_config = type('_C', (), {'cache_results': False})()
    client.search_cache = {}

    with pytest.raises(NotImplementedError, match="Phase 4B"):
        asyncio.run(client.search_alphavirus_literature("capsid protein"))
