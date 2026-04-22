"""SMTP notifier for run-state transitions (T08 AP §5.8).

Sends a plain-text email when a run enters a terminal or attention-
needed state (``completed`` / ``failed`` / ``paused_for_approval``).
The goal per AP §5.8 is "a scientist can ask 'is my job done' without
keeping the laptop open."

## Config

All SMTP parameters come from environment variables so deployments
at different institutions can point at their own mail infrastructure
without code changes:

- ``APECX_SMTP_HOST`` — SMTP server hostname (required to enable).
- ``APECX_SMTP_PORT`` — integer; default 587 (submission).
- ``APECX_SMTP_USER`` — SMTP auth username (optional for unauth'd
  local relays).
- ``APECX_SMTP_PASSWORD`` — SMTP auth password (optional).
- ``APECX_SMTP_USE_TLS`` — ``"true"`` for STARTTLS; default ``"true"``.
  Set ``"false"`` explicitly for localhost test servers.
- ``APECX_SMTP_FROM_ADDR`` — envelope-from + From: header.
- ``APECX_SMTP_TO_ADDR`` — default recipient for runs without a
  user-specific address. Real multi-user deployments override this
  via a per-user lookup (not implemented yet; logged in
  ``future_work.md``).

If ``APECX_SMTP_HOST`` is not set, the notifier is a no-op — it logs
the skip but does not raise. This matches the T09 / T10 / TX1
"scientist laptop default" story: the Control Plane comes up
without SMTP configured, and emails only start flowing when the
scientist (or their admin) provides a real SMTP endpoint.

## Integration surface

``EmailNotifier.send_state_transition(run_id, old_status, new_status,
user_id, run_summary)`` is the single public method. Callers are
expected to dispatch only for the three "interesting" transitions
listed above; the notifier still filters defensively so a wrongly-
routed call emits nothing rather than a spurious email.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from uuid import UUID

from apecx_integration.control_plane.schemas.enums import RunStatus

log = logging.getLogger(__name__)

NOTIFY_TRANSITIONS_TO: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.PAUSED,
    }
)


@dataclass(frozen=True, kw_only=True)
class SMTPConfig:
    host: str
    port: int = 587
    user: str | None = None
    password: str | None = None
    use_tls: bool = True
    from_addr: str = "apecx-cp@localhost"
    default_to_addr: str | None = None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_smtp_config_from_env() -> SMTPConfig | None:
    """Read SMTP params from the ``APECX_SMTP_*`` env vars. Returns
    ``None`` when ``APECX_SMTP_HOST`` is unset — the caller interprets
    this as "notifier disabled, silently no-op."
    """
    host = os.environ.get("APECX_SMTP_HOST")
    if not host:
        return None
    port_raw = os.environ.get("APECX_SMTP_PORT", "587")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"APECX_SMTP_PORT must be an integer, got {port_raw!r}") from exc
    return SMTPConfig(
        host=host,
        port=port,
        user=os.environ.get("APECX_SMTP_USER") or None,
        password=os.environ.get("APECX_SMTP_PASSWORD") or None,
        use_tls=_bool_env("APECX_SMTP_USE_TLS", True),
        from_addr=os.environ.get("APECX_SMTP_FROM_ADDR", "apecx-cp@localhost"),
        default_to_addr=os.environ.get("APECX_SMTP_TO_ADDR") or None,
    )


class EmailNotifier:
    """Sends run-state-transition emails via SMTP.

    Construct with an explicit :class:`SMTPConfig` (typically from
    :func:`load_smtp_config_from_env`). When ``config`` is ``None``
    the notifier is disabled and every call logs-and-returns.
    """

    def __init__(self, config: SMTPConfig | None) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config is not None

    def send_state_transition(
        self,
        *,
        run_id: UUID,
        old_status: RunStatus,
        new_status: RunStatus,
        user_id: str,
        to_addr: str | None = None,
        run_summary: str = "",
    ) -> bool:
        """Send an email for a run-state transition.

        Returns ``True`` when an email was actually dispatched,
        ``False`` when the notifier is disabled or the transition is
        one we deliberately don't email about (e.g., PENDING →
        RUNNING). Defensive filtering: calls for non-notify transitions
        return ``False`` without contacting SMTP.
        """
        if self._config is None:
            log.info(
                "EmailNotifier: SMTP disabled; skipping email for run %s " "%s -> %s",
                run_id,
                old_status.value,
                new_status.value,
            )
            return False
        if new_status not in NOTIFY_TRANSITIONS_TO:
            log.debug(
                "EmailNotifier: %s is not a notify transition; skipping",
                new_status.value,
            )
            return False

        resolved_to = to_addr or self._config.default_to_addr
        if not resolved_to:
            log.warning(
                "EmailNotifier: no recipient for run %s (user_id=%s); "
                "no APECX_SMTP_TO_ADDR default and no explicit to_addr. "
                "Skipping.",
                run_id,
                user_id,
            )
            return False

        msg = self._build_message(
            run_id=run_id,
            old_status=old_status,
            new_status=new_status,
            user_id=user_id,
            to_addr=resolved_to,
            run_summary=run_summary,
        )
        self._send(msg)
        log.info(
            "EmailNotifier: sent %s -> %s for run %s to %s",
            old_status.value,
            new_status.value,
            run_id,
            resolved_to,
        )
        return True

    def _build_message(
        self,
        *,
        run_id: UUID,
        old_status: RunStatus,
        new_status: RunStatus,
        user_id: str,
        to_addr: str,
        run_summary: str,
    ) -> EmailMessage:
        cfg = self._config
        assert cfg is not None  # enabled-guarded above
        msg = EmailMessage()
        subject_verb = {
            RunStatus.COMPLETED: "completed",
            RunStatus.FAILED: "failed",
            RunStatus.PAUSED: "paused for review",
        }.get(new_status, f"transitioned to {new_status.value}")
        msg["Subject"] = f"[apecx] run {run_id} {subject_verb}"
        msg["From"] = cfg.from_addr
        msg["To"] = to_addr
        body_lines = [
            f"Run id:      {run_id}",
            f"User:        {user_id}",
            f"Transition:  {old_status.value} -> {new_status.value}",
            "",
            f"Summary: {run_summary}" if run_summary else "Summary: (none)",
            "",
            "-- ",
            "This message was generated by the APECx Control Plane.",
            "See docs/future_work.md for the per-user notification-",
            "routing work that's still pending.",
        ]
        msg.set_content("\n".join(body_lines))
        return msg

    def _send(self, msg: EmailMessage) -> None:
        cfg = self._config
        assert cfg is not None
        with smtplib.SMTP(cfg.host, cfg.port, timeout=10) as client:
            if cfg.use_tls:
                client.starttls()
            if cfg.user:
                client.login(cfg.user, cfg.password or "")
            client.send_message(msg)
