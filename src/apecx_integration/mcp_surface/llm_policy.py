"""LLM-access resolution policy (design §9) — announce the resolved LLM, or LOUDLY REFUSE.

Before running a workflow whose LLM use cannot be omitted (an in-DAG LLM step, or any LLM
step in ``agent`` locus), the runner must RESOLVE and ANNOUNCE which LLM it will use, and
loudly refuse when none is resolvable — never silently degrade to an empty/null result.

Resolution (§3 L3): both loci resolve the SAME configured endpoint (``APECX_LLM_*``, via the
single-source ``_llm_config`` resolvers); they differ only in how it is ANNOUNCED — desktop
announces the *fallback* (D2: Claude Desktop has no MCP sampling, so the host can't be a
sub-step LLM; an in-DAG step still needs a configured local/external LLM), agent announces the
*server LLM*.

Availability is honest, not assumed:
- Ollama (local): available ⟺ the endpoint is reachable (probed via an HTTP GET).
- external API: available ⟺ an API key is configured (we do not probe an authed endpoint — a
  wrong key fails loudly at run time, a *different* failure than "no LLM configured").
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from apecx_integration.agents._llm_config import resolve_llm_base_url, resolve_llm_model
from apecx_integration.composition.runtime.execution_locus import ExecutionLocus


@dataclass(frozen=True)
class LlmResolution:
    """The outcome of resolving an LLM for an LLM-needing workflow."""

    available: bool
    target: str | None  # "ollama:nemotron-3-nano:4b" / "external:<model>" / None when unavailable
    detail: str  # the announcement (available) OR the loud-refusal reason (unavailable)


def _provider(base_url: str) -> str:
    host = base_url.lower()
    if any(tok in host for tok in ("localhost", "127.0.0.1", "11434", "ollama")):
        return "ollama"
    return "external"


def _probe_reachable(base_url: str, timeout: float = 2.0) -> bool:
    """Best-effort reachability probe (a real HTTP GET). Any failure → unreachable.

    Ollama's root returns 200 ("Ollama is running"), so a bare GET to the root (the base URL
    with a trailing ``/v1`` stripped) suffices.
    """
    root = base_url[:-3].rstrip("/") if base_url.rstrip("/").endswith("/v1") else base_url
    try:
        with urllib.request.urlopen(root, timeout=timeout):  # noqa: S310
            return True
    except Exception:  # noqa: BLE001 — unreachable/timeout/refused all mean "not available"
        return False


def resolve_llm(
    locus: ExecutionLocus,
    *,
    reachable: Callable[[str], bool] | None = None,
) -> LlmResolution:
    """Resolve the LLM for an LLM-needing workflow under ``locus``.

    Reads the SINGLE-SOURCE endpoint/model (``_llm_config.resolve_llm_base_url`` /
    ``resolve_llm_model``) so the announcement names exactly the model the runtime would call
    (no divergent hardcoded default). ``reachable`` is an injectable probe (default: a real
    HTTP GET) so the policy is unit-testable without a live endpoint.

    Returns ``available=False`` with a loud, actionable refusal reason when nothing resolves —
    the caller must refuse to run, not attempt-and-empty (§9).
    """
    base_url = resolve_llm_base_url().strip()
    model = resolve_llm_model().strip()
    api_key = os.environ.get("APECX_LLM_API_KEY")
    provider = _provider(base_url)
    target = f"{provider}:{model}"
    probe = reachable or _probe_reachable
    role = "fallback" if locus == ExecutionLocus.DESKTOP else "server LLM"

    if provider == "external":
        available = bool(api_key and api_key.strip())
        missing_hint = "set APECX_LLM_API_KEY (external endpoint requires a key)"
    else:
        available = probe(base_url)
        missing_hint = (
            "start a local Ollama server, or set APECX_LLM_BASE_URL to a reachable endpoint"
        )

    if available:
        return LlmResolution(
            available=True,
            target=target,
            detail=f"This workflow uses an LLM → resolved to {target} ({role}).",
        )
    return LlmResolution(
        available=False,
        target=None,
        detail=(
            f"This workflow needs an LLM but none is resolvable: {role} endpoint {base_url} "
            f"(model {model!r}) is unavailable. To fix: {missing_hint} "
            f"(APECX_LLM_BASE_URL / APECX_LLM_MODEL). It is refused rather than run to an "
            f"empty result (design §9)."
        ),
    )


def workflow_needs_llm_at_run(workflow: object, locus: ExecutionLocus) -> bool:
    """Does this LOADED workflow need an LLM to run UNDER ``locus``?

    ``agent`` locus: any LLM-bearing step needs the server LLM. ``desktop`` locus: a
    ``final_synthesis`` step omits its LLM call (the host synthesizes), so it does NOT need
    one — only a genuine in-DAG LLM step does. This is what keeps the gate from wrongly
    refusing a self-omitting workflow (e.g. viral_epitope_evidence_review) on a desktop with
    no Ollama.
    """
    from apecx_integration.composition.workflow_requires_llm import loaded_workflow_llm_steps

    steps = loaded_workflow_llm_steps(workflow)
    if locus == ExecutionLocus.DESKTOP:
        steps = [(n, r) for (n, r) in steps if r != "final_synthesis"]
    return len(steps) > 0


__all__ = ["LlmResolution", "resolve_llm", "workflow_needs_llm_at_run"]
