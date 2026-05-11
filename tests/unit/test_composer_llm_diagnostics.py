"""B2 — LLM-failure diagnostics.

When the provider raises during ``llm.invoke``, the composer must
log a single WARNING line carrying everything an operator needs to
debug:

  - model + base_url (catches misrouted-endpoint bugs immediately)
  - exception type + truncated message
  - elapsed time + message volume (catches "did we even reach
    the backend or did we time out before sending")
  - any HTTP response body the SDK attached (most SDKs expose it
    via ``exc.response`` / ``exc.body`` / ``exc.json``).

These tests pin the log shape so a future refactor that quietly
drops a field (which would land us right back at "Internal Server
Error with no signal") fails loudly.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from apecx_integration.composition.composer import Composer

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"


class _BoomLLM:
    """LLM stub whose invoke() raises a provider-like exception
    with an SDK-style ``response`` object attached. Mirrors the
    shape of ``httpx.HTTPStatusError`` / ``requests.HTTPError``
    so the composer's body-extraction code path is exercised.
    """

    class _FakeResponse:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text

    def invoke(self, messages):
        exc = RuntimeError("500 Internal Server Error from upstream")
        exc.response = self._FakeResponse(  # type: ignore[attr-defined]
            500,
            "upstream-error: model capacity exceeded; please retry in 30s",
        )
        raise exc


def test_failure_logs_full_diagnostics(caplog):
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: _BoomLLM()  # noqa: SLF001
    with (
        caplog.at_level(logging.WARNING, logger="apecx_integration.composition.composer"),
        pytest.raises(RuntimeError),
    ):
        asyncio.run(composer.compose("smoke prompt for diagnostics test"))
    records = [r for r in caplog.records if "Composer LLM call FAILED" in r.getMessage()]
    assert records, "no FAILED diagnostics line captured"
    msg = records[0].getMessage()
    # The five load-bearing fields from B2.
    assert "model=" in msg
    assert "base_url=" in msg
    assert "exc_type=RuntimeError" in msg
    assert "elapsed_ms=" in msg
    assert "total_in_chars=" in msg
    # The captured provider body must include the upstream text so
    # operators don't have to re-run with DEBUG to learn what the
    # backend said.
    assert "model capacity exceeded" in msg


class _NoBodyBoomLLM:
    """LLM whose exception has NO attached response — composer must
    still log diagnostics, just with provider_body=(none captured)."""

    def invoke(self, messages):
        raise ValueError("plain error with no SDK-attached body")


def test_failure_without_response_body_still_logs_diagnostics(caplog):
    composer = Composer.from_config(DEFAULT_CONFIG)
    composer._llm_factory = lambda **_kw: _NoBodyBoomLLM()  # noqa: SLF001
    with (
        caplog.at_level(logging.WARNING, logger="apecx_integration.composition.composer"),
        pytest.raises(ValueError),
    ):
        asyncio.run(composer.compose("another smoke prompt"))
    records = [r for r in caplog.records if "Composer LLM call FAILED" in r.getMessage()]
    assert records, "no FAILED diagnostics line captured"
    msg = records[0].getMessage()
    assert "provider_body=(none captured)" in msg
    assert "exc_type=ValueError" in msg
