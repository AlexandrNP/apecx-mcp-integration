"""T08 integration: EmailNotifier sends through a real in-process SMTP.

Uses aiosmtpd to spin up a throwaway SMTP server on localhost:<ephemeral-
port> and captures delivered messages for assertion. No external SMTP,
no mocks of the smtplib transport — real socket, real SMTP dialog.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from uuid import uuid4

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import Message
from apecx_integration.control_plane.notifications.email import (
    EmailNotifier,
    SMTPConfig,
    load_smtp_config_from_env,
)
from apecx_integration.control_plane.schemas.enums import RunStatus


def _pick_free_port() -> int:
    """Bind a throwaway socket to find an unused ephemeral port.

    aiosmtpd 1.4.x's ``Controller`` has a bug when used with
    ``port=0``: ``self.port`` isn't updated after the OS assigns a
    real port, so the post-bind self-check tries to connect to
    ``('127.0.0.1', 0)`` and fails with Errno 49. We sidestep by
    finding a free port ourselves and passing it explicitly. There's
    an unavoidable TOCTOU race between closing our probe socket and
    aiosmtpd binding, but with ephemeral ports the odds of collision
    are negligible at test volumes.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


pytestmark = pytest.mark.integration


class _CapturedHandler(Message):
    """Aiosmtpd handler that just keeps every delivered message in a list."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def handle_message(self, message) -> None:  # type: ignore[override]
        self.messages.append(message)


@contextlib.contextmanager
def _running_smtp():
    handler = _CapturedHandler()
    port = _pick_free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        yield controller, handler
    finally:
        controller.stop()


def _fresh_notifier(
    host: str, port: int, *, default_to: str | None = "alex@example.com"
) -> EmailNotifier:
    return EmailNotifier(
        SMTPConfig(
            host=host,
            port=port,
            use_tls=False,  # aiosmtpd default handler has no TLS
            from_addr="apecx-cp@test",
            default_to_addr=default_to,
        )
    )


def test_completed_transition_sends_email() -> None:
    with _running_smtp() as (ctl, handler):
        notifier = _fresh_notifier(ctl.hostname, ctl.port)
        sent = notifier.send_state_transition(
            run_id=uuid4(),
            old_status=RunStatus.RUNNING,
            new_status=RunStatus.COMPLETED,
            user_id="alex",
            run_summary="5/5 steps green",
        )
        # aiosmtpd delivers asynchronously in its own loop; give it a moment.
        asyncio.run(asyncio.sleep(0.05))

    assert sent is True
    assert len(handler.messages) == 1
    msg = handler.messages[0]
    assert "completed" in msg["Subject"]
    assert msg["To"] == "alex@example.com"
    body = msg.get_payload()
    assert "running -> completed" in body
    assert "alex" in body
    assert "5/5 steps green" in body


def test_failed_transition_sends_email() -> None:
    with _running_smtp() as (ctl, handler):
        notifier = _fresh_notifier(ctl.hostname, ctl.port)
        notifier.send_state_transition(
            run_id=uuid4(),
            old_status=RunStatus.RUNNING,
            new_status=RunStatus.FAILED,
            user_id="alex",
            run_summary="step 3 raised StepRejected",
        )
        asyncio.run(asyncio.sleep(0.05))
    assert len(handler.messages) == 1
    assert "failed" in handler.messages[0]["Subject"]


def test_paused_transition_sends_email() -> None:
    with _running_smtp() as (ctl, handler):
        notifier = _fresh_notifier(ctl.hostname, ctl.port)
        notifier.send_state_transition(
            run_id=uuid4(),
            old_status=RunStatus.RUNNING,
            new_status=RunStatus.PAUSED,
            user_id="alex",
            run_summary="synonym approval requested for 3 novel terms",
        )
        asyncio.run(asyncio.sleep(0.05))
    assert len(handler.messages) == 1
    assert "paused" in handler.messages[0]["Subject"].lower()


def test_uninteresting_transition_does_not_send() -> None:
    """PENDING -> RUNNING is not a notify-worthy transition. The
    notifier filters defensively — even if a caller dispatches this
    by mistake, no email is sent.
    """
    with _running_smtp() as (ctl, handler):
        notifier = _fresh_notifier(ctl.hostname, ctl.port)
        sent = notifier.send_state_transition(
            run_id=uuid4(),
            old_status=RunStatus.PENDING,
            new_status=RunStatus.RUNNING,
            user_id="alex",
        )
        asyncio.run(asyncio.sleep(0.05))
    assert sent is False
    assert handler.messages == []


def test_explicit_to_addr_overrides_default() -> None:
    with _running_smtp() as (ctl, handler):
        notifier = _fresh_notifier(ctl.hostname, ctl.port, default_to="default@ex")
        notifier.send_state_transition(
            run_id=uuid4(),
            old_status=RunStatus.RUNNING,
            new_status=RunStatus.COMPLETED,
            user_id="alex",
            to_addr="override@ex",
        )
        asyncio.run(asyncio.sleep(0.05))
    assert handler.messages[0]["To"] == "override@ex"


def test_no_recipient_skips_gracefully() -> None:
    """default_to_addr is None AND no explicit to_addr → log + skip,
    don't raise and don't send.
    """
    with _running_smtp() as (ctl, handler):
        notifier = _fresh_notifier(ctl.hostname, ctl.port, default_to=None)
        sent = notifier.send_state_transition(
            run_id=uuid4(),
            old_status=RunStatus.RUNNING,
            new_status=RunStatus.COMPLETED,
            user_id="alex",
        )
        asyncio.run(asyncio.sleep(0.05))
    assert sent is False
    assert handler.messages == []


def test_disabled_notifier_is_no_op() -> None:
    """When no SMTPConfig is supplied (env var unset in production),
    the notifier is constructed but every send is a no-op.
    """
    notifier = EmailNotifier(config=None)
    assert notifier.enabled is False
    sent = notifier.send_state_transition(
        run_id=uuid4(),
        old_status=RunStatus.RUNNING,
        new_status=RunStatus.COMPLETED,
        user_id="alex",
    )
    assert sent is False


def test_load_smtp_config_from_env_returns_none_without_host(monkeypatch) -> None:
    monkeypatch.delenv("APECX_SMTP_HOST", raising=False)
    assert load_smtp_config_from_env() is None


def test_load_smtp_config_from_env_reads_all_fields(monkeypatch) -> None:
    monkeypatch.setenv("APECX_SMTP_HOST", "mail.example.org")
    monkeypatch.setenv("APECX_SMTP_PORT", "2525")
    monkeypatch.setenv("APECX_SMTP_USER", "apecx")
    monkeypatch.setenv("APECX_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("APECX_SMTP_USE_TLS", "false")
    monkeypatch.setenv("APECX_SMTP_FROM_ADDR", "no-reply@example.org")
    monkeypatch.setenv("APECX_SMTP_TO_ADDR", "alex@example.org")
    cfg = load_smtp_config_from_env()
    assert cfg is not None
    assert cfg.host == "mail.example.org"
    assert cfg.port == 2525
    assert cfg.user == "apecx"
    assert cfg.password == "secret"
    assert cfg.use_tls is False
    assert cfg.from_addr == "no-reply@example.org"
    assert cfg.default_to_addr == "alex@example.org"


def test_load_smtp_config_rejects_non_integer_port(monkeypatch) -> None:
    monkeypatch.setenv("APECX_SMTP_HOST", "mail.example.org")
    monkeypatch.setenv("APECX_SMTP_PORT", "not-a-number")
    with pytest.raises(ValueError, match="must be an integer"):
        load_smtp_config_from_env()
