"""MCP surface entry point — FastMCP-based server.

Registers the tool modules and runs the stdio transport so Claude
Desktop (or any MCP client) can invoke them. Reads
``APECX_CONTROL_PLANE_URL`` (default ``http://localhost:8000``)
to decide where the backend lives.

Running
-------
    python -m apecx_integration.mcp_surface.server

The server speaks MCP over stdio; Claude Desktop's client wires it
automatically when configured.

Backend autostart (added 2026-04-27 — config-only-deployment UX)
----------------------------------------------------------------
When ``APECX_MCP_AUTOSTART_BACKEND=1`` (the recommended default for
Claude Desktop installs) and the Control Plane is unreachable at
startup, this module spawns ``apecx-cp serve`` as a child process,
polls ``/healthz`` until it answers, and registers an atexit hook
to terminate the child on MCP-server exit. The child uses SQLite
for state by default — no Docker / Postgres required for the
single-user case the MCP target audience runs.

What's exposed
--------------
- ``start_workflow`` / ``show_diff`` / ``execute_workflow``
- ``list_pending_approvals`` / ``approve`` / ``reject`` / ``correct``
- ``estimate_cost`` / ``confirm_allocation``
- ``export_hpc_bundle`` / ``ingest_hpc_bundle``

What's deliberately NOT exposed
-------------------------------
- ``/hpc/submit`` — still 501 at the Control Plane (needs T04/T05
  executor runtime). A tool that always errors is strictly worse
  than "tool absent."
- ``create_approval`` — internal, called by nanobrain's
  ApprovalStep during workflow execution, not a scientist-facing
  tool.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from apecx_integration.mcp_surface.locus import (
    ExecutionLocus,
    resolve_locus,
    set_active_locus,
)
from apecx_integration.mcp_surface.network_config import (
    NetworkConfig,
    get_network_config,
    load_network_config,
    set_network_config,
)
from apecx_integration.mcp_surface.tools import (
    database_tools,
)
from apecx_integration.mcp_surface.tools import (
    discovery as discovery_tools,
)
from apecx_integration.mcp_surface.tools import (
    eo_primitives as eo_primitive_tools,
)
from apecx_integration.mcp_surface.tools import (
    harmonized_search as harmonized_search_tools,
)
from apecx_integration.mcp_surface.tools import (
    infrastructure_status as infrastructure_status_tool,
)
from apecx_integration.mcp_surface.tools import (
    workflows as workflow_tools,
)

# Note: ``get_client`` is intentionally NOT imported here. The
# startup health check builds an ephemeral client (see
# ``_verify_control_plane_reachable``) so it doesn't pollute the
# tool-call singleton with a dead-loop binding.

log = logging.getLogger(__name__)


_CHILD_BACKEND_PROC: subprocess.Popen[bytes] | None = None
_CHILD_BACKEND_LOG: Path | None = None


# Top-level MCP instructions — surfaced to the client (Claude Desktop) in the
# `initialize` response. This is the ONE global "what is this server for + when to
# use it" signal. Without it, a capable desktop LLM answers a viral-immunology
# question from its own training data and never considers these tools relevant (the
# "LLM won't trigger discovery" failure). The text trades on user-domain INTENT
# (viruses / vaccines / epitopes / structures / sequences), not the internal jargon
# of "workflows" / "composer" — the model routes on intent, not implementation.
_SERVER_INSTRUCTIONS = """\
apecx-mcp answers viral-immunology questions — about viruses, vaccines, pathogens, \
genes and proteins, epitopes and antigens, protein structures, and sequence \
conservation — with GROUNDED, CITED evidence assembled from real curated sources \
(BV-BRC, VIOLIN, PDB/EMDB, PubMed, and the APECx harvested Globus corpus). This is \
data you cannot get from training knowledge.

When the user asks any such question, do NOT answer from general knowledge first. \
Instead:
  1. Call `apecx_capabilities` (or `list_workflows`) to see the specialized \
workflows and primitive tools available, and which are runnable right now.
  2. Run the best-matching workflow with `run_workflow(name, params)` and synthesize \
from its grounded, cited result.
  3. For a one-off "find records about entity X across the curated indexes" lookup, \
call `harmonized_search` directly.
  4. If no catalog workflow fits the question, `compose_workflow` builds a new one.

Pass the user's plain-English question (or a bare entity name) as `query` — the \
workflow resolves names to taxa itself. Do NOT write or run scripts, and do NOT call \
BV-BRC / Globus / PubMed / the databases directly: `run_workflow` performs all \
retrieval and analysis and returns the finished report in its `markdown` field. There \
is ONE workflow-run tool (`run_workflow`); it streams per-stage progress automatically \
in desktop.

Prefer these tools over answering from memory whenever the question touches viral \
immunology, vaccines, pathogen genomics, or protein/epitope structure."""


def build_server(locus: ExecutionLocus | None = None) -> FastMCP:
    """Construct the FastMCP server with every tool registered.

    Split from ``main()`` so tests can exercise the server without
    launching the stdio transport.

    ``locus`` selects the orchestration face (``desktop`` ↔ ``agent``); it comes from
    the ``apecx-mcp --locus`` startup flag (``$APECX_EXECUTION_LOCUS`` is a fallback).
    Resolved here so a misconfigured value FAILS LOUD at startup, recorded
    process-wide (``set_active_locus``) so steps + ``run_workflow`` read it, and stored
    on the server as ``server.execution_locus`` for introspection.
    """
    resolved_locus = locus if locus is not None else resolve_locus()
    set_active_locus(resolved_locus)
    log.info("server build: execution_locus=%s", resolved_locus.value)

    server: FastMCP = FastMCP("apecx-mcp", instructions=_SERVER_INSTRUCTIONS)
    server.execution_locus = resolved_locus  # type: ignore[attr-defined]

    _register_logging_capability(server)
    _register_reasoning_prompts(server)

    # Composer — ONE primitive (2026-06-15 Layer-1 trim; was start_workflow /
    # show_diff / execute_workflow, which split one decision across 3 tools and
    # competed with run_workflow). compose_workflow folds the T06 diff-review in
    # and returns control to the caller before execution. Use ONLY when
    # list_workflows finds no existing catalog workflow.
    server.tool()(workflow_tools.compose_workflow)

    # Discovery — read-only catalog tools that let the model see
    # which workflows / components the composer can actually build
    # before it calls compose_workflow.
    server.tool()(discovery_tools.list_workflows)
    server.tool()(discovery_tools.describe_workflow)

    # EO thin-surface primitives (external_orchestration_design.md §4) —
    # "workflows as first-class objects": the external LLM discovers a
    # workflow (list_workflows) then drives ANY of them through this generic
    # set, instead of one bespoke super-tool per task.
    # ONE tool to run any workflow. FastMCP injects a Context for desktop clients, so
    # run_workflow streams each reasoning stage automatically there; headless callers
    # (ctx=None) get the same result one-shot. No separate streaming tool to choose.
    server.tool()(eo_primitive_tools.run_workflow)
    server.tool()(eo_primitive_tools.inspect_run)
    server.tool()(eo_primitive_tools.inspect_workflow)
    server.tool()(eo_primitive_tools.apecx_context)
    # "What can I do now + what infra unlocks more" — aggregates list_workflows
    # prerequisites + infrastructure_status backends into one capability view,
    # queryable directly from the desktop MCP client.
    server.tool()(eo_primitive_tools.apecx_capabilities)
    # Operator HITL approval for the evidence workflow's design/optimization output. The
    # design gate issues a scope-bound `dapprv-…` token (fail-closed); the operator approves
    # it here, then the caller re-calls run_workflow with that design_approval_id.
    server.tool()(eo_primitive_tools.approve_design)

    # Direct database lookups — bypass the composer for one-shot
    # VIOLIN + BV-BRC queries the model can answer without orchestrating
    # a workflow ("list vaccines targeting EEEV").
    # Database query primitives — DELIBERATELY NOT EXPOSED as MCP tools
    # (2026-06-09 tier cleanup).
    #
    # query_vaccines / query_pathogens / query_genes / query_bvbrc_genomes /
    # get_vaccine_pathogen_genes / resolve_entity / resolve_canonical_entity
    # were registered as tier-1 tools but violated the workflow-first
    # tiered architecture: each one has a specific first-line description
    # (e.g. "Search the BV-BRC alphavirus genome database (~17,000
    # genomes)") that LLMs route against in preference to the canonical
    # ``harmonized_search`` workflow tool. The result was that the LLM
    # would pick the cheaper-looking primitive — bypassing harmonized
    # synonym expansion + the verdict surface + (until the 2026-06-09
    # Path B hotfix) HITL gating on ambiguous terms.
    #
    # ``harmonized_search`` (below) is now the canonical entry point for
    # "find records about X in any APECx Globus index" queries. The
    # underlying Python functions remain importable and are used
    # internally by the synthesis pipeline and by the composer.
    #
    # ``database_statistics`` STAYS on the wire as a meta/navigation
    # tool — it doesn't compete with harmonized_search; it tells the LLM
    # "what tables exist, what columns they have" for composer-driven
    # workflow generation.
    server.tool()(database_tools.database_statistics)

    # ``synthesize_query`` was RETIRED 2026-06-15 (Layer-1 surface trim): it was a
    # super-tool (the design calls for "primitives, not super-tools") that
    # duplicated ``run_workflow`` and whose retrieval used the legacy local-CSV +
    # raw-Globus path instead of harmonized search. Grounded synthesis is provided
    # by the catalog workflows (``viral_epitope_analysis`` does it WITH
    # harmonized search). See docs/layer1_surface_trim_plan.md.

    # Viral epitope / immunology analysis is provided by the
    # ``viral_epitope_analysis`` CATALOG WORKFLOW (discoverable via
    # ``list_workflows``, run via ``run_workflow``) — a multi-step,
    # harmonized-search pipeline (PDB/EMDB structural evidence + nested
    # sequence-conservation over harmonized BV-BRC + grounded synthesis). The
    # former standalone ``analyze_viral_immunology`` / ``analyze_eeev_epitopes``
    # tools were RETIRED 2026-06-15: they bypassed harmonized search (raw Globus
    # free-text + local CSVs), were not discoverable as workflows, and their
    # builder/YAML no longer loaded. Use the workflow.

    # Globus Search — DELIBERATELY NOT EXPOSED as an MCP tool.
    #
    # query_globus_search was the raw free-text passthrough to the APECx
    # Globus index — no entity resolution, no HITL gating, no
    # harmonization. An LLM picking it for "RSV" got a mix of all 6
    # RSV-disambiguated organisms with no signal that disambiguation was
    # needed. ``harmonized_search`` (below) supersedes it: same Globus
    # index, structured verdict surface, HITL gate on ambiguous terms.
    #
    # The underlying Python function ``globus_search_tools.query_globus_search``
    # remains importable and is used internally by the synthesis pipeline.
    # Removed from the MCP surface on 2026-06-09 — see ``tools/_hitl_gate.py``
    # for the architectural rationale.

    # Harmonized search — drives the harmonized_search nanobrain workflow:
    # term → canonical IRI → per-index filter → raw-vs-harmonized
    # comparison with HITL gating on ambiguous resolution. The opinionated
    # harmonization path. See ``composition/workflows/harmonized_search/``
    # for the workflow YAML.
    server.tool()(harmonized_search_tools.harmonized_search)

    # Operational control-plane tools — REMOVED from the agentic MCP surface
    # 2026-06-15 (Layer-1 trim, external_orchestration_design.md §4: "operational
    # tools … out of the agentic loop"). The control-plane routes, client, and
    # schemas remain — these operations are driven via the control-plane API /
    # `apecx-cp`, not by the orchestrating LLM:
    #   - approvals: list_pending_approvals / approve / reject / correct
    #     (the WORKFLOW design-gate keeps its own `approve_design` tool above).
    #   - HPC: estimate_cost / confirm_allocation / export_hpc_bundle /
    #     ingest_hpc_bundle — to be re-exposed as ONE `hpc_export` catalog
    #     workflow (follow-up; see docs/layer1_surface_trim_plan.md Phase 3).

    # Infrastructure-status — reports the health of the 5 backends
    # apecx-mcp depends on (Postgres, Redis, MinIO, Ollama, Rhea MCP).
    # The orchestrator runs in the background (scheduled below); this
    # tool reads its state on every call and re-probes ready backends
    # so a died-mid-session backend is reported as degraded — never
    # stale green. See ``infrastructure/orchestrator.py``.
    server.tool()(infrastructure_status_tool.infrastructure_status)

    # Pre-made nanobrain workflows — generalized catalog-driven
    # registration. Each entry in the catalog YAML becomes ONE MCP
    # tool whose name, description, and input schema come from the
    # catalog (NOT from a hand-written Python function). FAIL-LOUD:
    # a broken catalog raises at startup; an entry whose prereqs are
    # unmet registers with [UNAVAILABLE: ...] in its description and
    # returns an actionable error on call. See
    # ``mcp_surface/workflow_registry.py`` +
    # ``docs/running_nanobrain_workflows_via_mcp.md``.
    from apecx_integration.mcp_surface.workflow_registry import (
        load_catalog,
        register_workflows,
    )

    catalog_path = os.environ.get("APECX_MCP_WORKFLOW_CATALOG")
    catalog = load_catalog(catalog_path)
    report = register_workflows(server, catalog, logger=log)
    log.info("workflow registry: %s", report.summary_line())

    # Infrastructure orchestrator — fire-and-forget bring-up of the 5
    # backends (Postgres, Redis, MinIO, Ollama, Rhea MCP). Runs in a
    # daemon thread so the FastMCP startup is not blocked waiting for
    # slow probes / spawns. The ``infrastructure_status`` MCP tool
    # (registered above) reads the orchestrator's state at call time
    # and re-probes ready backends — there is no stale-green path.
    #
    # ``APECX_MCP_AUTOSTART_INFRA=0`` switches the orchestrator into
    # probe-only mode (read on first construction inside
    # ``get_orchestrator``); ``start_all`` still runs so the probes
    # populate state, but no ``docker run`` / ``Popen`` is invoked.
    # G88 (2026-05-16): auto-discover the Rhea checkout + apply
    # platform-aware defaults to any unset RHEA_* env vars BEFORE the
    # orchestrator starts. The orchestrator's existing rhea_mcp
    # auto-spawn logic engages only when RHEA_REPO_PATH +
    # RHEA_PYTHON_PATH are populated; pre-G88 that was operator-only.
    # With this hook, an operator who has the rhea checkout next to
    # apecx-mcp-integration (the standard workspace layout) and ran
    # `apecx-setup rhea` once gets rhea-server auto-started here.
    # Opt out via APECX_RHEA_AUTODISCOVER=0.
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        autodiscover_rhea_env,
        autodiscovery_enabled,
    )

    if autodiscovery_enabled():
        discovered = autodiscover_rhea_env()
        if discovered:
            log.info(
                "MCP startup: rhea env autodiscovery set %d var(s): %s",
                len(discovered),
                sorted(discovered.keys()),
            )
        else:
            log.debug(
                "MCP startup: rhea env autodiscovery — no changes (operator pre-set or no rhea repo found)"
            )

    # Startup sweep: bound the per-run artifacts folder (a prior process may have left many dirs).
    try:
        from apecx_integration.mcp_surface.tools.eo_primitives import prune_artifacts

        prune_artifacts()
    except Exception as exc:  # noqa: BLE001 — a retention sweep must never block startup
        log.warning("MCP startup: artifact retention sweep failed: %s", exc)

    from apecx_integration.infrastructure.orchestrator import (
        start_orchestrator_in_background_thread,
    )

    start_orchestrator_in_background_thread()
    autostart = os.environ.get("APECX_MCP_AUTOSTART_INFRA", "1") != "0"
    log.info(
        "MCP startup: infrastructure orchestrator launched in background thread (autostart=%s).",
        autostart,
    )

    return server


def _register_logging_capability(server: FastMCP) -> None:
    """Make ``logging/setLevel`` a real, advertised MCP method.

    FastMCP wires tools/resources/prompts but NOT the ``logging``
    capability. Without a registered ``SetLevelRequest`` handler, a
    standards-compliant client (Claude Desktop, or any client that
    negotiates a log level during/after ``initialize``) gets
    ``McpError: Method not found`` from ``session.set_logging_level(...)``,
    which tears down the whole session BEFORE the streaming tool runs.

    Registering a handler on the underlying low-level server — the same
    ``_mcp_server`` seam FastMCP uses for its own ``list_tools`` /
    ``call_tool`` handlers — does two things:

    1. ``get_capabilities()`` flips ``logging`` on (it advertises the
       capability iff a ``SetLevelRequest`` handler exists), so the
       client's negotiation sees the method as supported.
    2. ``logging/setLevel`` requests now resolve to this handler and
       return ``EmptyResult`` (ok) instead of "Method not found".

    The level is accepted-but-ignored: stage delivery rides
    ``ServerSession.send_log_message``, which does NOT gate on a
    configured level, so the per-stage streaming contract is unchanged
    whether or not a client ever sets a level. See
    ``docs/desktop_streaming_contract.md``.
    """
    from mcp.types import LoggingLevel

    @server._mcp_server.set_logging_level()
    async def _accept_logging_level(level: LoggingLevel) -> None:
        log.debug("MCP set_logging_level: accepted level=%s (no-op)", level)


def _register_reasoning_prompts(server: FastMCP) -> None:
    """Serve the desktop reasoning host's operating rules as live MCP prompts.

    The connected host (Claude Desktop / IDE) is the orchestrating + synthesizing LLM in
    desktop locus. ``instructions=`` is advisory and short; these two prompts carry the
    detailed reuse-first loop the host should follow — fetchable on demand rather than
    always-resident. Both are pinned by ``tests/unit/test_reasoning_prompts.py`` so a future
    edit cannot silently drop a load-bearing imperative.

    Loaded at registration time (not lazily) so a missing/renamed asset FAILS LOUD at server
    build, not on a client's first prompt fetch.
    """
    from apecx_integration.mcp_surface.prompts import load_prompt

    rules = load_prompt("rules_core.md")
    protocol = load_prompt("reasoning_protocol.md")

    @server.prompt(
        name="reasoning_rules",
        description="Lean imperative rules for the apecx reasoning host (reuse-first, "
        "closed-class, nanobrain framework rules, requires_llm).",
    )
    def reasoning_rules() -> str:
        return rules

    @server.prompt(
        name="reasoning_protocol",
        description="The match → parametrize → execute procedure for fulfilling a "
        "viral-immunology task with the apecx tools.",
    )
    def reasoning_protocol() -> str:
        return protocol


async def _ping_control_plane(base_url: str) -> bool:
    """Hit /healthz — return True iff the backend answers."""
    from apecx_integration.mcp_surface.control_plane_client import (
        ControlPlaneClient,
    )

    ephemeral = ControlPlaneClient(base_url)
    try:
        await ephemeral.healthz()
        return True
    except Exception:  # noqa: BLE001 — any wire error is "unreachable"
        return False
    finally:
        await ephemeral.close()


def _autostart_backend(base_url: str) -> subprocess.Popen[bytes] | None:
    """Spawn ``apecx-cp serve`` as a child process.

    Returns the Popen handle on success; None on failure (in which
    case the caller should fall back to the eager-fail path).

    Lifecycle: child is registered via atexit so it terminates when
    the MCP server exits. Stderr lands in a log file under
    ``$TMPDIR/apecx-cp-autostart.log`` so the operator can debug
    backend issues without losing the stdout MCP stream.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8000

    # Refuse to autostart against a non-loopback host. Spawning a
    # backend bound to 0.0.0.0 / a public IP from inside an MCP
    # client is a significant security shape — the operator should
    # do that explicitly.
    if host not in {"127.0.0.1", "localhost", "::1"}:
        log.error(
            "MCP autostart: refusing to spawn backend for "
            "non-loopback URL %s. Set control_plane.host to a loopback "
            "address in your apecx config, or start the backend manually "
            "and set APECX_MCP_AUTOSTART_BACKEND=0.",
            base_url,
        )
        return None

    # Resolve ``apecx-cp`` relative to THIS process's interpreter
    # before falling back to anything else. Using ``shutil.which`` as
    # the first choice was a real cross-environment bug
    # (2026-05-15 clean-install verification): a user with a stale
    # `~/.local/bin/apecx-cp` from an older `pipx install` had that
    # one resolved first by PATH, so the autostarted Control Plane
    # would run an older version that mismatched the freshly-
    # installed apecx-mcp's API expectations — silent shape drift.
    #
    # ``sys.executable`` is the Python actually running THIS module;
    # its sibling ``bin/apecx-cp`` is the binary that ships with
    # the same package install. That's the "use my own venv's
    # binary, not whatever's on PATH" rule.
    venv_bin = Path(sys.executable).parent
    venv_cp = venv_bin / "apecx-cp"
    if venv_cp.is_file() and os.access(venv_cp, os.X_OK):
        cp_binary = str(venv_cp)
        cp_args = ["serve", "--host", host, "--port", str(port)]
    else:
        # The venv binary is missing (e.g., the user installed via
        # ``pip install --no-scripts`` or set ``console_scripts``
        # off in a custom setup). Fall back to the same Python
        # interpreter + module form — same package, same code path,
        # just no shim script. ``shutil.which`` is INTENTIONALLY
        # NOT consulted: a stale PATH apecx-cp would defeat the
        # whole point of this fix.
        cp_binary = sys.executable
        cp_args = [
            "-m",
            "apecx_integration.control_plane.app",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ]

    import tempfile

    log_path = Path(tempfile.gettempdir()) / "apecx-cp-autostart.log"
    log_fh = log_path.open("ab")
    log.info(
        "MCP autostart: spawning backend (%s %s) — child stderr -> %s",
        cp_binary,
        " ".join(cp_args),
        log_path,
    )

    # Inherit env (LLM vars + APECX_CP_POSTGRES_URL if set). Force
    # SQLite default by NOT setting POSTGRES URL when absent.
    proc = subprocess.Popen(
        [cp_binary, *cp_args],
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=log_fh,
        # Detach from MCP server's process group so child doesn't
        # receive the SIGINT MCP server gets on Ctrl-C from Claude
        # Desktop. We terminate it explicitly via atexit.
        start_new_session=True,
    )

    global _CHILD_BACKEND_PROC, _CHILD_BACKEND_LOG
    _CHILD_BACKEND_PROC = proc
    _CHILD_BACKEND_LOG = log_path
    atexit.register(_terminate_child_backend)
    return proc


def _terminate_child_backend() -> None:
    """atexit handler — SIGTERM the child backend with a 5s grace,
    then SIGKILL if it lingers."""
    proc = _CHILD_BACKEND_PROC
    if proc is None or proc.poll() is not None:
        return  # already exited
    log.info("MCP shutdown: terminating autostart backend (pid=%s)", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        log.warning("MCP shutdown: backend did not terminate within 5s; sending SIGKILL")
        proc.kill()
        proc.wait(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("MCP shutdown: error during backend cleanup: %s", exc)


async def _wait_for_backend_ready(
    base_url: str,
    *,
    timeout_s: float = 60.0,
) -> bool:
    """Poll /healthz until it answers or timeout. Returns True on
    success, False on timeout. 60s default covers SQLite migrations
    on a cold first run."""
    deadline = time.monotonic() + timeout_s
    poll_interval = 0.5
    while time.monotonic() < deadline:
        if await _ping_control_plane(base_url):
            return True
        # If the child died early, fail fast — no point polling.
        if _CHILD_BACKEND_PROC and _CHILD_BACKEND_PROC.poll() is not None:
            return False
        await asyncio.sleep(poll_interval)
    return False


async def _verify_control_plane_reachable() -> None:
    """Ensure the Control Plane is reachable, autostarting it if
    configured to do so.

    Behavior matrix:

      ============================  ===============  ============================
      APECX_MCP_SKIP_HEALTHCHECK    Backend up        Action
      ============================  ===============  ============================
      1                             —                 Skip; warn-log only
      0 (default)                   yes               Log success
      0 (default)                   no, autostart=1   Spawn backend; poll; succeed
                                                      OR exit(2) on poll timeout
      0 (default)                   no, autostart=0   Exit(2) with a
                                                      ``control_plane`` config
                                                      remediation hint
      ============================  ===============  ============================

    Audit §3.2: pre-fix the lazy client meant a misconfigured URL
    only surfaced when a scientist actually invoked a tool. Eager-
    failing at startup gives the operator an immediate, actionable
    error.
    """
    if os.environ.get("APECX_MCP_SKIP_HEALTHCHECK") == "1":
        log.info("MCP startup: APECX_MCP_SKIP_HEALTHCHECK=1, skipping CP reachability check.")
        return

    base_url = get_network_config().control_plane_url

    if await _ping_control_plane(base_url):
        log.info("MCP startup: Control Plane at %s reachable.", base_url)
        return

    autostart_enabled = os.environ.get("APECX_MCP_AUTOSTART_BACKEND", "1") != "0"
    if not autostart_enabled:
        log.error(
            "MCP startup: Control Plane at %s is unreachable AND "
            "APECX_MCP_AUTOSTART_BACKEND=0. Either start the "
            "backend manually (`apecx-cp serve`), set "
            "APECX_MCP_AUTOSTART_BACKEND=1, or set "
            "APECX_MCP_SKIP_HEALTHCHECK=1 to bypass the check.",
            base_url,
        )
        raise SystemExit(2)

    log.info(
        "MCP startup: Control Plane at %s unreachable; autostarting "
        "backend (set APECX_MCP_AUTOSTART_BACKEND=0 to disable).",
        base_url,
    )
    proc = _autostart_backend(base_url)
    if proc is None:
        log.error(
            "MCP startup: autostart failed — could not locate the "
            "apecx-cp binary or refused to spawn. See the autostart "
            "log path printed above; verify ``apecx-cp`` is on PATH "
            "or in the same venv as ``apecx-mcp``."
        )
        raise SystemExit(2)

    if not await _wait_for_backend_ready(base_url, timeout_s=60.0):
        # Surface the child's stderr tail so the operator sees what
        # actually went wrong (port conflict, missing dep, etc.).
        log.error(
            "MCP startup: autostart spawned backend (pid=%s) but it "
            "did not become ready within 60s. Backend log: %s",
            proc.pid,
            _CHILD_BACKEND_LOG,
        )
        if _CHILD_BACKEND_LOG and _CHILD_BACKEND_LOG.is_file():
            try:
                tail = _CHILD_BACKEND_LOG.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )[-2000:]
                log.error("Backend log tail:\n%s", tail)
            except Exception:  # noqa: BLE001
                pass
        raise SystemExit(2)

    log.info(
        "MCP startup: autostart succeeded — backend at %s, child pid=%s",
        base_url,
        proc.pid,
    )


def _check_data_root_or_warn() -> None:
    """Log a loud banner when the VIOLIN/BV-BRC data dir is missing.

    The MCP server itself starts fine without data — start_workflow
    and the approval/HPC tools still work — but every database tool
    (query_vaccines, etc.) returns ``{"error": ...}``.  Operators who
    install via ``uv tool install`` (bypassing scripts/install.sh)
    have no other signal that they skipped ``apecx-setup``; surface
    the missing piece in the MCP server log so it's visible in
    ``~/Library/Logs/Claude/mcp-server-apecx.log``.
    """
    # Local import: keep server boot lean for paths that never use db.
    from apecx_integration.mcp_surface.data.database import _resolve_data_root

    data_root = _resolve_data_root()
    if data_root is None:
        reason = "neither APECX_DATA_ROOT nor APECX_ROOT is set in the MCP server's env block"
    elif not data_root.is_dir():
        reason = f"configured data root {data_root} does not exist"
    else:
        violin_dir = data_root / "violin"
        bvbrc_csv = data_root / "BVBRC_genome_alphavirus.csv"
        if not violin_dir.is_dir() and not bvbrc_csv.is_file():
            reason = (
                f"data root {data_root} exists but contains neither "
                f"violin/ nor BVBRC_genome_alphavirus.csv"
            )
        else:
            return  # data present — silent success.

    log.warning("=" * 64)
    log.warning("APECx data tools DISABLED — %s", reason)
    log.warning("")
    log.warning("Database tools (query_vaccines, query_pathogens, …) will")
    log.warning("return errors on every call until this is fixed.")
    log.warning("")
    log.warning("To fix:")
    log.warning("  1. Run ``apecx-setup`` to download the dataset")
    log.warning("     (one-time, ~1.5 MB).")
    log.warning("  2. Set APECX_DATA_ROOT in your Claude Desktop config:")
    log.warning('       "env": { "APECX_DATA_ROOT": "<path>", ... }')
    log.warning("  3. Fully quit and relaunch Claude Desktop.")
    log.warning("")
    log.warning("Log path (macOS): ~/Library/Logs/Claude/mcp-server-apecx.log")
    log.warning("=" * 64)


def _check_rag_index_or_warn() -> None:
    """Log a loud banner when the domain RAG index is missing (G81).

    The MCP server boots fine without RAG — synthesis pipelines that
    wire ``DomainRagSearchStep`` or ``SynthesisContextAssemblyStep``
    keep running because the leaf ``DomainRagIndex.search`` returns
    ``[]`` with its own once-per-process warning. But operators who
    install via ``uv tool install`` (bypassing ``apecx-setup``) have
    no other signal that RAG is silently disabled until they read
    their workflow run logs and notice the empty ``rag_chunks``
    bundles. Surface the missing piece in the MCP server log so it's
    visible in ``~/Library/Logs/Claude/mcp-server-apecx.log`` at boot.

    Cheap: one stat call per file. Does NOT load FAISS or the
    sentence-transformer model.
    """
    from apecx_integration.agents.domain_rag import DomainRagIndex

    # Default index dir — same resolution as DomainRagIndex's no-arg
    # constructor. We don't need to know which workflow YAML overrides
    # this; the default location is what apecx-setup builds into.
    idx = DomainRagIndex()
    if idx.is_available:
        log.info("MCP startup: domain RAG index detected at %s", idx.index_dir)
        return

    log.warning("=" * 64)
    log.warning("RAG DISABLED — domain RAG index not present at %s", idx.index_dir)
    log.warning("")
    log.warning("Synthesis workflows that wire RAG branches will run, but the")
    log.warning("RAG branch will return empty chunks for every query.")
    log.warning("Pipelines do NOT crash — they degrade gracefully.")
    log.warning("")
    log.warning("To enable RAG:")
    log.warning("  apecx-setup rag       # interactive builder (recommended)")
    log.warning("or, directly:")
    log.warning("  PYTHONPATH=src .venv/bin/python scripts/build_domain_rag_index.py")
    log.warning("")
    log.warning("RAG is OPTIONAL since 2026-05-16 (G81). All non-RAG workflow")
    log.warning("paths are unaffected — DB queries, MCP tools, composer, HPC")
    log.warning("execution, synonym dictionary, etc. all work without it.")
    log.warning("=" * 64)


def _check_rhea_status_or_warn() -> None:
    """Log a banner reporting the Rhea bring-up state at MCP startup (G89).

    Cheap stat-only probes — does NOT touch the orchestrator's live
    rhea-server probe (that runs in the background thread and surfaces
    via the ``infrastructure_status`` MCP tool). What we surface here
    is the STATIC state ``apecx-setup rhea`` would have produced, so an
    operator seeing the boot logs can immediately diagnose why Rhea
    isn't auto-spawning if they expected it to.

    Three states:
      * ``Rhea: checkout missing`` — autodiscovery couldn't find the
        rhea repo. Rhea-backed tools (muscle, future Galaxy tools)
        will be UNAVAILABLE via the MCP catalog.
      * ``Rhea: checkout found but venv missing`` — operator hasn't
        run ``apecx-setup rhea`` yet. Same UNAVAILABLE state.
      * ``Rhea: ready`` — bring-up done; the orchestrator's
        background thread will auto-spawn rhea-server.
    """
    from apecx_integration.infrastructure.rhea_env_autodiscovery import (
        _find_rhea_repo,
    )

    rhea_repo = _find_rhea_repo()
    if rhea_repo is None:
        log.warning("=" * 64)
        log.warning("Rhea: checkout NOT FOUND in standard probe locations")
        log.warning("")
        log.warning("Rhea-backed bioinformatics tools (e.g. muscle) will be")
        log.warning("UNAVAILABLE via the MCP catalog until you:")
        log.warning("  1. git clone https://github.com/AlexandrNP/rhea.git")
        log.warning("     (into the workspace next to apecx-mcp-integration/)")
        log.warning("  2. apecx-setup rhea")
        log.warning("  3. restart this MCP server")
        log.warning("")
        log.warning("If you don't need Rhea-backed tools, this banner is benign.")
        log.warning("=" * 64)
        return

    venv = rhea_repo / ".venv" / "bin" / "python"
    if not venv.exists():
        log.warning("=" * 64)
        log.warning("Rhea: checkout at %s but venv NOT BUILT", rhea_repo)
        log.warning("")
        log.warning("Rhea-backed bioinformatics tools will be UNAVAILABLE.")
        log.warning("To enable: `apecx-setup rhea` then restart this MCP server.")
        log.warning("=" * 64)
        return

    log.info(
        "Rhea: ready (checkout=%s, venv=%s) — InfraOrchestrator will auto-spawn rhea-server",
        rhea_repo,
        venv.parent,
    )


def _try_public_download(sqlite_path: Path) -> Path | None:
    """Anonymous bootstrap of the dictionary from the public Globus path.

    Tries to fetch the pre-built dictionary via
    :func:`apecx_harvesters.dict_reader.bootstrap.bootstrap_dictionary` —
    the canonical clean-install path. Anonymous HTTPS only; no Globus
    credentials, no keyring access, no env-var pre-configuration
    required (the bootstrap defaults to the production URL when
    ``APECX_DICT_PUBLIC_BASE_URL`` is unset).

    Returns the dictionary path on success, ``None`` on any failure
    (network, sha mismatch, unsupported schema, dict_reader missing).
    The caller then falls back to the local-build path so offline /
    dev environments still work.

    Opt out via ``APECX_SKIP_DICT_DOWNLOAD=1`` to force the local-build
    path (useful when the published version is older than what the
    local build would produce).
    """
    if os.environ.get("APECX_SKIP_DICT_DOWNLOAD", "").strip() == "1":
        log.info(
            "APECX_SKIP_DICT_DOWNLOAD=1 — skipping public bootstrap, "
            "deferring to local-build fallback"
        )
        return None
    try:
        from apecx_harvesters.dict_reader.bootstrap import (
            bootstrap_dictionary,
        )
    except ImportError as exc:
        log.info(
            "public download path unavailable (apecx_harvesters.dict_reader "
            "import failed: %s) — deferring to local build",
            exc,
        )
        return None
    try:
        log.warning(
            "MCP startup: bootstrapping synonym dictionary from public "
            "Globus path (anonymous, ~30s first run) to %s",
            sqlite_path,
        )
        return bootstrap_dictionary(dest=sqlite_path, quiet=True)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "public dictionary bootstrap failed (%s) — falling back to "
            "local-build path; entity resolution will use slow substring "
            "search if neither path produces an artifact",
            exc,
        )
        return None


def _ensure_synonym_dict_or_warn() -> None:
    """Make the synonym dictionary available at startup — DOWNLOAD it from Globus.

    The dictionary is a PRE-BUILT artifact published on Globus; it is DOWNLOADED, not built on
    a user machine. The single clean-install path:

    1. **Public download** (anonymous). Fetches the pre-built SQLite from the canonical Argonne
       LCF Globus HTTPS path. Cached on disk thereafter. Opt out via ``APECX_SKIP_DICT_DOWNLOAD=1``.

    If the download produces no file, this FAILS LOUD with an actionable message — it does NOT
    silently fall back to a slow local build (which needs VIOLIN data a clean install lacks). A
    local build is available ONLY as an explicit dev/offline opt-in via
    ``APECX_DICT_ALLOW_LOCAL_BUILD=1``. If ``APECX_SYNONYM_DICT_PATH`` already points at an
    existing file, the download is skipped (the operator's choice wins).

    After a path produces a SQLite, the loader singleton is pre-warmed so the first MCP tool
    call doesn't pay the open cost.
    """
    from apecx_integration.synonym_dictionary.loader import (
        configure_dictionary_path,
        get_dictionary_index,
    )
    from apecx_integration.synonym_dictionary.workflow.bootstrap import (
        EnsureDictionaryConfig,
        ensure_dictionary,
    )

    cfg = EnsureDictionaryConfig().resolve()
    assert cfg.sqlite_path is not None  # resolve() guarantees this

    if not cfg.sqlite_path.is_file():
        # Path 1: public anonymous download (no creds, no env config).
        downloaded = _try_public_download(cfg.sqlite_path)
        if downloaded is not None:
            os.environ["APECX_SYNONYM_DICT_PATH"] = str(downloaded)

    if not cfg.sqlite_path.is_file() and os.environ.get("APECX_DICT_ALLOW_LOCAL_BUILD") == "1":
        # The published dictionary is DOWNLOADED from Globus — it is NOT built on a user machine
        # (a local build needs VIOLIN data a clean install does not have). So the local build is
        # OPT-IN for dev/offline ONLY (APECX_DICT_ALLOW_LOCAL_BUILD=1) — never an automatic
        # fallback that silently masks a download failure.
        log.info("MCP startup: download produced no file; APECX_DICT_ALLOW_LOCAL_BUILD=1 → build")
        try:
            built = ensure_dictionary(cfg)
        except Exception as exc:  # noqa: BLE001 — opt-in dev path; surface + continue
            built = None
            log.warning("opt-in local dictionary build failed: %s", exc)
        if built is not None:
            os.environ["APECX_SYNONYM_DICT_PATH"] = str(built)

    if not cfg.sqlite_path.is_file():
        # LOUD + actionable — the Globus download did not produce a file and no dict is present.
        # Do NOT silently degrade: name the failure, the consequence, and the fix. (Search still
        # works in a degraded mode — it returns UNHARMONIZED raw full-text matches — but
        # taxon-precise resolution is OFF until the dictionary is downloaded.)
        log.warning("=" * 70)
        log.warning("SYNONYM DICTIONARY UNAVAILABLE — the Globus download produced no file.")
        log.warning("  Expected at: %s", cfg.sqlite_path)
        log.warning("  Consequence: entity resolution + taxon-precise harmonized search are OFF;")
        log.warning("    harmonized_search returns UNHARMONIZED raw full-text matches meanwhile.")
        log.warning("  Fix: ensure network access to the Argonne LCF Globus HTTPS endpoint, then")
        log.warning("    re-run `apecx-setup dict` or restart apecx-mcp. To use an existing copy:")
        log.warning("    APECX_SYNONYM_DICT_PATH=/path/to/dictionary.sqlite. (Was the download")
        log.warning("    disabled? APECX_SKIP_DICT_DOWNLOAD must be unset. Dev/offline build:")
        log.warning("    APECX_DICT_ALLOW_LOCAL_BUILD=1 + local VIOLIN data.)")
        log.warning("=" * 70)
        return
        os.environ["APECX_SYNONYM_DICT_PATH"] = str(built)

    # Pre-warm the loader singleton so the first MCP tool call doesn't pay
    # the SQLite open + manifest validation cost.
    configure_dictionary_path(cfg.sqlite_path)
    _, err = get_dictionary_index()
    if err:
        log.warning("=" * 64)
        log.warning("Synonym dictionary failed to load: %s", err)
        log.warning("Entity resolution will fall back to slow substring search.")
        log.warning("=" * 64)
    else:
        log.info("Synonym dictionary loaded from %s", cfg.sqlite_path)


# Backwards-compat alias kept for tests that still import the old name.
_check_synonym_dict_or_warn = _ensure_synonym_dict_or_warn


_HELP_EPILOG = """\
Ports + hosts come from the centralized config file (--config / $APECX_CONFIG /
~/.apecx/config.yml; built-in defaults apply when absent). Set mcp.host/mcp.port and
control_plane.host/control_plane.port there — there are NO env vars for them. See the
packaged config.yml.example.

Environment variables (honored at startup):

  APECX_MCP_SKIP_HEALTHCHECK   When set to "1", skip the Control
                               Plane reachability check at startup.

  APECX_MCP_AUTOSTART_BACKEND  When "0", do NOT auto-spawn the
                               Control Plane backend if it isn't
                               already reachable. Default: 1 (auto).

  APECX_MCP_AUTOSTART_INFRA    When "0", run the infra orchestrator
                               in probe-only mode (no container or
                               Rhea-MCP autostart). Default: 1.

  APECX_DATA_ROOT              Path to the VIOLIN/BV-BRC data dir
                               (enables the direct DB lookup tools).

  APECX_SYNONYM_DICT_PATH      Override the synonym dictionary SQLite
                               path. Default is the one apecx-setup
                               provisions.

  APECX_MCP_WORKFLOW_CATALOG   Override the packaged catalog of MCP-
                               exposed workflows. Path to YAML.

  RHEA_MCP_URL                 Where the Rhea MCP probe connects. DERIVED
                               from the config's rhea.host/port at startup
                               and exported for the Rhea machinery — set
                               rhea.host/port in the config, not this var.

  RHEA_REPO_PATH, RHEA_PYTHON_PATH, RHEA_CONDA_BIN, RHEA_CONDA_ENVS_DIR
                               Required (and orchestrator-set) for
                               Rhea autostart + tool conda env unpack.

  See docs/apecx_mcp_infrastructure.md §3 for the full table.
"""


def _resolve_package_version() -> str:
    """Return the installed package version, or 'unknown' if unresolvable.

    Uses ``importlib.metadata`` so the version reflects what `pip`
    actually installed — not a hardcoded string that could drift from
    the wheel's recorded version.
    """
    try:
        from importlib.metadata import version

        return version("apecx-integration")
    except Exception:  # noqa: BLE001
        # importlib.metadata may raise PackageNotFoundError when the
        # package isn't pip-installed by name (development from-source
        # via PYTHONPATH only). 'unknown' is honest — better than
        # fabricating a version.
        return "unknown"


def _build_arg_parser():
    """Argparse for ``apecx-mcp``.

    Why argparse (not click): the binary is invoked by Claude Desktop
    over stdio with NO arguments — argparse's zero-arg path is the
    same hot path as click's, and argparse is stdlib (no extra runtime
    dep). The flag surface is minimal on purpose: this binary IS the
    MCP server entry, configuration is env-var-driven (operator
    workflow uses ``.env`` / docker-compose / Claude Desktop's MCP
    server config), and the only useful flags are ``--help`` (this
    function exists for) and ``--version`` (so operators can see what
    they have installed).
    """
    parser = argparse.ArgumentParser(
        prog="apecx-mcp",
        description=(
            "APECX MCP server (stdio transport). Boots the FastMCP "
            "tool surface (24 tools across workflows / discovery / "
            "database / canonical entity / synthesis / approvals / "
            "HPC), runs the Control Plane health check (auto-spawning "
            "the backend if needed), boots the infra orchestrator, "
            "and lazy-builds the synonym dictionary on first run. "
            "Configuration is env-var-driven; see the epilog."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"apecx-mcp (apecx-integration {_resolve_package_version()})",
        help="print the apecx-integration package version and exit",
    )
    parser.add_argument(
        "--locus",
        choices=[m.value for m in ExecutionLocus],
        default=None,
        help=(
            "orchestration face: 'desktop' (default) — the host LLM (Claude Desktop) "
            "synthesizes; a workflow's final-synthesis LLM is omitted and the assembled "
            "evidence is returned for the host. 'agent' — the apecx server LLM synthesizes "
            "(headless). Falls back to $APECX_EXECUTION_LOCUS, then 'desktop'."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help=(
            "MCP transport. 'stdio' (default) — for Claude Desktop / IDE clients that spawn the "
            "server locally. 'streamable-http' — serve over HTTP at /mcp, REQUIRED by ChatGPT "
            "and for remote/server deployment (see `apecx-setup chatgpt`). 'sse' — legacy "
            "HTTP+SSE transport. (For HTTP transports set the bind address with --host/--port, or "
            "mcp.host/mcp.port in ~/.apecx/config.yml.)"
        ),
    )
    # HTTP-transport bind address. apecx owns this resolution (vs FastMCP's version-fragile
    # $FASTMCP_HOST/$FASTMCP_PORT, observed NOT to take effect in a real deployment): a flag wins,
    # else the config's mcp.host/mcp.port (defaults 127.0.0.1 / 8001). Ignored for stdio (no socket).
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "Bind address for HTTP transports (streamable-http / sse). Defaults to mcp.host in the "
            "apecx config (127.0.0.1). Use 0.0.0.0 to accept remote connections. Ignored for stdio."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Port for HTTP transports (streamable-http / sse). Defaults to mcp.port in the apecx "
            "config (8001), distinct from the control plane (8000). Ignored for stdio."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to the apecx network config (ports + hosts). Defaults to $APECX_CONFIG, then "
            "~/.apecx/config.yml; built-in defaults apply when absent. See the packaged "
            "config.yml.example."
        ),
    )
    return parser


def _resolve_http_host_port(
    args: argparse.Namespace, config: NetworkConfig | None = None
) -> tuple[str, int]:
    """Resolve the HTTP-transport bind (host, port) with precedence **CLI flag > config > built-in**.

    There is NO env var for these — the centralized config file (``~/.apecx/config.yml``, see
    ``network_config``) is the single source; ``mcp.host`` / ``mcp.port`` carry the defaults
    (127.0.0.1 / 8001, the latter distinct from the control plane's 8000). apecx resolves the bind
    itself rather than deferring to FastMCP's ``$FASTMCP_HOST/$FASTMCP_PORT``, which proved
    unreliable in a real deployment (the operator had to monkeypatch ``server.settings`` by hand).

    The config port is range-validated by the pydantic model; an explicit ``--port`` is range-checked
    here so a bad CLI value still FAILS LOUD instead of binding nonsense.
    """
    cfg = config or get_network_config()
    host = args.host or cfg.mcp.host
    port = args.port if args.port is not None else cfg.mcp.port
    if not (1 <= port <= 65535):
        raise ValueError(f"port must be in 1..65535, got {port}")
    return host, port


def _assert_mcp_port_distinct_from_control_plane(
    http_host: str, http_port: int, config: NetworkConfig | None = None
) -> None:
    """FAIL-LOUD (before the boot) when the MCP HTTP bind collides with the local control-plane port.

    The MCP server autostarts + POSTs to the control plane (config ``control_plane.host/port``,
    default ``127.0.0.1:8000``). If the MCP HTTP transport binds that same local port, uvicorn cannot
    bind it — but only AFTER a full ~30s boot, as a cryptic "address already in use" that also tears
    down the autostarted control plane. They are different services and must use different ports
    (deploy/SERVER_DEPLOYMENT.md); surface it instantly with an actionable message.

    Only a LOCAL collision counts: a remote control plane (non-loopback host) or an MCP bound to a
    specific non-loopback interface does not contend for the same socket.

    ``_LOOPBACK`` deliberately includes ``0.0.0.0`` — a wildcard bind contends with a loopback one
    (so it is broader than the autostart-refusal set near the top of this module, which omits it).
    Two edges are intentionally left to uvicorn's own bind error as a backstop: a specific-interface
    MCP host vs a ``0.0.0.0``-bound CP (they DO contend), and ``::1`` vs ``127.0.0.1`` (different
    address families that do NOT contend — this over-fires, erring toward a clear fail-fast).
    """
    cfg = config or get_network_config()
    cp_host = cfg.control_plane.host.lower()
    cp_port = cfg.control_plane.port
    mcp_host = http_host.lower()
    _LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    if http_port == cp_port and cp_host in _LOOPBACK and mcp_host in _LOOPBACK:
        raise ValueError(
            f"MCP HTTP port {http_port} collides with the control plane "
            f"(control_plane.port={cp_port} in your apecx config). The MCP server and the control "
            f"plane are different services and must use different ports — set mcp.port in the config "
            f"(or pass --port {http_port + 1}) to a distinct value, or move the control plane. "
            f"See deploy/SERVER_DEPLOYMENT.md."
        )


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``apecx-mcp`` console script.

    Behavior:

    * ``apecx-mcp --help`` prints usage + env-var table and exits 0.
    * ``apecx-mcp --version`` prints the installed package version and
      exits 0.
    * ``apecx-mcp`` (no args) runs the server: Control Plane health-
      check (auto-spawn backend if needed), data-root warn, synonym-
      dict bootstrap, then ``server.run()`` over stdio.

    Args:
        argv: Command-line argument list, defaulting to ``sys.argv[1:]``.
            Tests pass ``[]`` for the default-args case or
            ``["--help"]`` / ``["--version"]`` to exercise the flag
            paths. The console-script entry point leaves ``argv=None``
            so argparse reads ``sys.argv``.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)  # exits on --help / --version
    # FAIL-LOUD at startup on a bad locus (argparse already constrains --locus; this also
    # validates a stray $APECX_EXECUTION_LOCUS before the server boots).
    resolved_locus = resolve_locus(args.locus)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    # Load the centralized network config (precedence CLI --config > $APECX_CONFIG >
    # ~/.apecx/config.yml > built-in defaults) and set it process-wide so every consumer reads ONE
    # source. A malformed config / unknown key FAILS LOUD here, before the boot.
    network_config = load_network_config(args.config)
    set_network_config(network_config)

    # Rhea is wired through the load-bearing $RHEA_MCP_URL interface (RheaAdapter.from_env, the
    # workflow-YAML ${RHEA_MCP_URL} interpolation, the infra probe). Make the config the SOURCE by
    # deriving + exporting it here — operators set rhea.host/port in the config, not this env var.
    os.environ["RHEA_MCP_URL"] = network_config.rhea_mcp_url

    # E4-1a — durable HITL design approvals by default for the long-lived server: persist
    # issued/approved tokens so they survive a restart. Done in the entry point (NOT
    # build_server, which tests call) so unit tests stay in-memory + pollution-free. An
    # operator override of APECX_DESIGN_APPROVAL_DIR wins via setdefault.
    from pathlib import Path as _Path

    from apecx_integration.composition.runtime.design_approval_store import (
        DESIGN_APPROVAL_DIR_ENV,
    )

    os.environ.setdefault(
        DESIGN_APPROVAL_DIR_ENV, str(_Path.home() / ".cache" / "apecx" / "design_approvals")
    )

    # Resolve + validate the HTTP bind UPFRONT — before the ~30s boot and before autostarting the
    # control plane — so a port collision fails instantly with an actionable message instead of a
    # cryptic late uvicorn bind error that also tears down the autostarted control plane.
    http_host: str | None = None
    http_port: int | None = None
    if args.transport != "stdio":
        http_host, http_port = _resolve_http_host_port(args, network_config)
        _assert_mcp_port_distinct_from_control_plane(http_host, http_port, network_config)

    asyncio.run(_verify_control_plane_reachable())
    _check_data_root_or_warn()
    _check_rag_index_or_warn()
    _check_rhea_status_or_warn()
    _ensure_synonym_dict_or_warn()
    server = build_server(locus=resolved_locus)
    if args.transport == "stdio":
        server.run()
    else:
        # HTTP transports (ChatGPT / remote deployment need streamable-http at /mcp). Apply the
        # apecx-resolved host/port (computed + collision-checked upfront) to FastMCP's settings
        # BEFORE run() — see _resolve_http_host_port for why we don't defer to $FASTMCP_*. A PUBLIC
        # HTTPS URL still requires a tunnel (see `apecx-setup chatgpt`).
        if http_host is not None:
            server.settings.host = http_host
        assert (
            http_port is not None
        )  # always resolved for HTTP above (defaults to 8001) — narrow type
        server.settings.port = http_port
        log.info(
            "apecx-mcp serving %s at http://%s:%s%s",
            args.transport,
            server.settings.host,
            server.settings.port,
            getattr(server.settings, "streamable_http_path", "/mcp"),
        )
        server.run(transport=args.transport)


if __name__ == "__main__":
    main()
