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
leak. **Durable (E4-1a):** pass ``persist_dir`` (the server defaults
``$APECX_DESIGN_APPROVAL_DIR`` to ``~/.cache/apecx/design_approvals``) and issued/approved
tokens are mirrored to ``<dir>/<token>.json`` + reloaded on construction, so they survive a
server restart. ``persist_dir=None`` (the default for a bare ``DesignApprovalStore()``) stays
in-memory — which keeps unit tests pollution-free.

**Threat-model boundary (be honest about what this enforces):** this closes the
"any non-blank string opens the gate" bypass — design output now requires an explicit
request → approve → re-call with a scope-bound token. The STRENGTH of the approval then
depends on WHO can call ``approve_design``: in the intended HUMAN-OPERATED MCP-client model
(a scientist drives the client; the orchestrating LLM proposes, the human approves) it is a
genuine human-in-the-loop gate. In a fully-AUTONOMOUS deployment where the LLM itself can
call ``approve_design``, the LLM can self-approve — so the gate is advisory there, not
enforcing. This is the SAME posture as the existing control-plane ``approve``/``reject``
MCP tools (approval-as-an-MCP-tool); a human-only approval surface / auth is a cross-cutting
follow-up for the whole approval system, not specific to design approvals.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 1000


@dataclass
class DesignApprovalRecord:
    """One issued design-approval token + its decision state."""

    token: str
    scope: tuple[str, str]  # (query_norm, protein_norm) — the design-request identity
    status: str  # "pending" | "approved" | "rejected"
    decided_by: str | None = None

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "scope": list(self.scope),
            "status": self.status,
            "decided_by": self.decided_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DesignApprovalRecord:
        scope = d["scope"]
        return cls(
            token=d["token"],
            scope=(scope[0], scope[1]),
            status=d["status"],
            decided_by=d.get("decided_by"),
        )


class DesignApprovalStore:
    """Thread-safe, FIFO-bounded store of design-approval tokens.

    In-memory by default (``persist_dir=None``). When ``persist_dir`` is given, every mutation
    (request/approve/reject) is also written to ``<persist_dir>/<token>.json`` and existing
    records are loaded on construction — so tokens survive an MCP-server restart (E4-1a). The
    in-memory dict is the authoritative fast path; disk is a durable mirror. FIFO order across
    restarts is reconstructed from file mtime (no schema field needed)."""

    def __init__(
        self,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        *,
        persist_dir: str | os.PathLike | None = None,
    ) -> None:
        if max_tokens < 1:
            raise ValueError(f"DesignApprovalStore max_tokens must be >= 1, got {max_tokens}")
        # RLock so a future approve()-under-request() path cannot self-deadlock (cf. the
        # SynonymOverlay non-reentrant-lock deadlock, 2026-06-12).
        self._lock = threading.RLock()
        self._by_token: dict[str, DesignApprovalRecord] = {}
        self._max = max_tokens
        self._dir: Path | None = Path(persist_dir) if persist_dir else None
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk()

    # ------------------------------------------------------------------
    # Durability (no-ops when persist_dir is None)
    # ------------------------------------------------------------------
    def _load_from_disk(self) -> None:
        """Load persisted records oldest-first (by mtime) so dict-insertion order — which the
        FIFO bound relies on — matches creation order across restarts. A corrupt file is
        skipped LOUD (degrade-loud), never silently dropping an approval."""
        assert self._dir is not None
        for p in sorted(self._dir.glob("*.json"), key=lambda q: q.stat().st_mtime):
            try:
                rec = DesignApprovalRecord.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                log.warning("DesignApprovalStore: skipping unreadable token file %s (%s)", p, exc)
                continue
            self._by_token[rec.token] = rec
        while len(self._by_token) > self._max:
            oldest = next(iter(self._by_token))
            self._evict_file(oldest)
            del self._by_token[oldest]

    def _persist(self, rec: DesignApprovalRecord) -> None:
        if self._dir is None:
            return
        final = self._dir / f"{rec.token}.json"
        tmp = self._dir / f".{rec.token}.json.tmp"
        try:
            tmp.write_text(json.dumps(rec.to_dict(), sort_keys=True), encoding="utf-8")
            os.replace(tmp, final)  # atomic
        except Exception as exc:  # noqa: BLE001 — persistence failure must not break issuance
            log.warning("DesignApprovalStore: failed to persist token %s (%s)", rec.token, exc)

    def _evict_file(self, token: str) -> None:
        if self._dir is None:
            return
        try:
            (self._dir / f"{token}.json").unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("DesignApprovalStore: failed to evict token file %s (%s)", token, exc)

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
            rec = DesignApprovalRecord(
                token=token, scope=self._scope(query, protein), status="pending"
            )
            self._by_token[token] = rec
            self._persist(rec)
            # FIFO-bound (dict is insertion-ordered → first key is oldest).
            while len(self._by_token) > self._max:
                oldest = next(iter(self._by_token))
                self._evict_file(oldest)
                del self._by_token[oldest]
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
            self._persist(rec)
            return rec

    def reject(self, token: str, *, decided_by: str = "operator") -> DesignApprovalRecord | None:
        with self._lock:
            rec = self._by_token.get(token)
            if rec is None:
                return None
            rec.status = "rejected"
            rec.decided_by = decided_by
            self._persist(rec)
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
        """Test hook + session reset. Also removes persisted files so a durable store is
        truly reset (a test that clears then expects empty must not see stale disk state)."""
        with self._lock:
            if self._dir is not None:
                for tok in list(self._by_token):
                    self._evict_file(tok)
            self._by_token.clear()


_singleton_lock = threading.Lock()
_singleton: DesignApprovalStore | None = None

#: Env var that turns on durable persistence for the process-wide store. UNSET → in-memory
#: (the default — keeps tests pollution-free). The MCP server sets it (or an operator does)
#: to a writable dir so design approvals survive a server restart (E4-1a).
DESIGN_APPROVAL_DIR_ENV = "APECX_DESIGN_APPROVAL_DIR"


def get_design_approval_store() -> DesignApprovalStore:
    """Process-wide design-approval store singleton.

    Durable iff ``$APECX_DESIGN_APPROVAL_DIR`` is set + non-empty (else in-memory). Read once
    at first access; the MCP server sets the env (defaulting it) before the store is touched."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            persist_dir = os.environ.get(DESIGN_APPROVAL_DIR_ENV) or None
            _singleton = DesignApprovalStore(persist_dir=persist_dir)
        return _singleton
