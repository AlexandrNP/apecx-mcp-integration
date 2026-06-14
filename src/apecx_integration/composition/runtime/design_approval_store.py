"""Design-approval store — server-ISSUED, scoped, fail-closed HITL tokens for the
evidence workflow's design/optimization output (2026-06-14).

Closes the design-gate bypass: before this, ``DesignGateStep`` opened on ANY non-blank
``design_approval_id`` (a presence check — a caller could pass any string). This store
makes the token a server-issued, operator-approved, scope-bound credential.

**Why a dedicated store, not the control-plane ``Approval`` model:** the control-plane
approval entity is run/step-centric (``CreateApprovalRequest`` requires ``run_id`` +
``step_id`` referencing a control-plane *execution*), but the evidence workflow runs via
the direct MCP ``run_workflow`` path and has NO control-plane run/step context. Forcing
that model would require synthesizing run/step identities. Per the closed-class rule
(author a new class when an existing component genuinely does not fit), this store is the
right-sized fit.

**Lifecycle:** the gate ISSUES a token (``request``) bound to the design REQUEST scope
``(query, protein)`` when design is requested without a valid approval; an operator
APPROVES it explicitly (``approve``, surfaced as the ``approve_design`` MCP tool); the gate
opens ONLY for a token that ``validate`` confirms is approved AND whose scope matches the
current request. **Fail-closed:** an unknown / unapproved / scope-mismatched token withholds
design — a token approved for one design request can never open a different one.

In-process + bounded (FIFO, like RunStore/HandleStore) — the long-lived MCP server must not
leak. v1 is session-scoped (a durable backend is a documented swap-in, same as those stores).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

_DEFAULT_MAX_TOKENS = 1000


@dataclass
class DesignApprovalRecord:
    """One issued design-approval token + its decision state."""

    token: str
    scope: tuple[str, str]  # (query_norm, protein_norm) — the design-request identity
    status: str  # "pending" | "approved" | "rejected"
    decided_by: str | None = None


class DesignApprovalStore:
    """In-memory, thread-safe, FIFO-bounded store of design-approval tokens."""

    def __init__(self, max_tokens: int = _DEFAULT_MAX_TOKENS) -> None:
        if max_tokens < 1:
            raise ValueError(f"DesignApprovalStore max_tokens must be >= 1, got {max_tokens}")
        # RLock so a future approve()-under-request() path cannot self-deadlock (cf. the
        # SynonymOverlay non-reentrant-lock deadlock, 2026-06-12).
        self._lock = threading.RLock()
        self._by_token: dict[str, DesignApprovalRecord] = {}
        self._max = max_tokens

    @staticmethod
    def _scope(query: str | None, protein: str | None) -> tuple[str, str]:
        """Normalize the design-request identity. Whitespace-collapsed + lowercased so
        trivial formatting differences don't defeat the scope match, but a genuinely
        different query/protein is a different scope."""
        return (
            " ".join((query or "").split()).lower(),
            " ".join((protein or "").split()).lower(),
        )

    def request(self, *, query: str | None, protein: str | None) -> str:
        """Issue a fresh PENDING token bound to ``(query, protein)``; return the token."""
        with self._lock:
            token = "dapprv-" + uuid4().hex
            self._by_token[token] = DesignApprovalRecord(
                token=token, scope=self._scope(query, protein), status="pending"
            )
            # FIFO-bound (dict is insertion-ordered → first key is oldest).
            while len(self._by_token) > self._max:
                del self._by_token[next(iter(self._by_token))]
            return token

    def approve(self, token: str, *, decided_by: str = "operator") -> DesignApprovalRecord | None:
        """Mark ``token`` approved. Returns the record, or ``None`` if the token is unknown
        (loud — the caller surfaces "unknown design approval token", never a silent pass)."""
        with self._lock:
            rec = self._by_token.get(token)
            if rec is None:
                return None
            rec.status = "approved"
            rec.decided_by = decided_by
            return rec

    def reject(self, token: str, *, decided_by: str = "operator") -> DesignApprovalRecord | None:
        with self._lock:
            rec = self._by_token.get(token)
            if rec is None:
                return None
            rec.status = "rejected"
            rec.decided_by = decided_by
            return rec

    def get(self, token: str) -> DesignApprovalRecord | None:
        with self._lock:
            return self._by_token.get(token)

    def validate(
        self, *, token: str | None, query: str | None, protein: str | None
    ) -> tuple[bool, str]:
        """FAIL-CLOSED scope-bound validation. Returns ``(ok, reason)``: ``ok`` is True ONLY
        when the token exists, is approved, and its issued scope matches THIS request. Every
        other case (blank, unknown, pending/rejected, scope mismatch) returns False + a NAMED
        reason for the gate to surface — never a silent pass."""
        if token is None or (isinstance(token, str) and not token.strip()):
            return False, "no design_approval_id was provided"
        with self._lock:
            rec = self._by_token.get(token)
        if rec is None:
            return False, (
                "design_approval_id is unknown — it was not issued by this server "
                "(a token must be requested + approved, not fabricated)"
            )
        if rec.status != "approved":
            return False, f"design_approval_id status is {rec.status!r}, not 'approved'"
        if rec.scope != self._scope(query, protein):
            return False, (
                "design_approval_id was issued for a DIFFERENT design request "
                "(query/protein scope mismatch) — approvals are not transferable"
            )
        return True, "approved"

    def clear(self) -> None:
        """Test hook + session reset."""
        with self._lock:
            self._by_token.clear()


_singleton_lock = threading.Lock()
_singleton: DesignApprovalStore | None = None


def get_design_approval_store() -> DesignApprovalStore:
    """Process-wide design-approval store singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = DesignApprovalStore()
        return _singleton
