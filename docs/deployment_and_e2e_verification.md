# apecx-mcp — Fresh-Environment Deployment & End-to-End Verification

A runbook for deploying apecx-mcp into a **new environment** and verifying that the entire
setup + every tool works. It is organized as: (1) what gets deployed, (2) the install chain,
(3) start-up, (4) end-to-end verification (per-backend, per-tool, sandbox, dashboard), and
(5) the automated gated test suite that encodes those checks.

> Scope: this describes the **integrated system** — the state of `main` after the flakiness
> arc + #7 (Ollama-as-container) + the infra dashboard + #1c (novel-step sandbox) all merge.
> Commands assume the `apecx-mcp-integration` package is installed and its `.venv` (or a fresh
> venv) is active. Run tests via `PYTHONPATH=src .venv/bin/python -m pytest` or
> `scripts/run_tests.sh`.

---

## 1. What gets deployed

| Layer | Component | Notes |
|---|---|---|
| Tier-1 MCP | `apecx-mcp` (FastMCP) | scientist-facing tools over stdio (or `--transport streamable-http`). `--locus desktop` (default) or `--locus agent`. |
| Tier-2 control plane | `apecx-cp serve` (FastAPI) | run state, provenance, approvals, artifacts; hosts the **infra dashboard** (`GET /status`, `GET /dashboard`) + the always-on monitor daemon. |
| Backends (docker) | postgres (pgvector), redis, minio, **ollama** (#7), rhea_mcp | managed by `InfraOrchestrator`; bound to **127.0.0.1** (loopback) by default. |
| LLM | Ollama container (local) OR `APECX_LLM_BASE_URL` (remote) | #7: local ⇒ managed container; model-aware readiness (a model-less server reads DEGRADED). |
| Sandbox (#1c) | `apecx-novel-sandbox:1.0` image | executes composer "novel Python" steps in a hardened container (`--network none`, read-only, non-root, cap-drop ALL). |
| Data | BV-BRC (required) + VIOLIN (optional) via Globus | `apecx-setup data`; Globus native device-code login by default. |
| Synonym dict / RAG | sqlite dict (lazy build) / FAISS domain index (opt-in) | dict builds on first MCP start; RAG via `apecx-setup rag`. |

---

## 2. Fresh-environment install chain

### 2.0 Prerequisites
- **Docker** running (Desktop or engine). `docker info` must exit 0.
- **Python 3.12**, `git`.
- (Optional) a Globus identity in the `apecx-project-all` group for VIOLIN.

### 2.1 Install the package
```bash
# isolated tool install (recommended for operators)
uv tool install apecx-mcp-integration
# OR a dev checkout
git clone <repo> && cd apecx-mcp-integration && pip install -e '.[dev]'
```
> **Venv hygiene (F1).** A deployment MUST use a FRESH venv. Do NOT reuse a developer's `.venv`: it is
> often an editable install pointing at *some* worktree's `src/`, so `apecx-mcp` there runs whatever
> branch that worktree is on — not what you think you deployed. Verify with
> `python -c "import apecx_integration; print(apecx_integration.__file__)"` → it must resolve under the
> venv's `site-packages/`, never a `…/wt-*/src/` path.

### 2.2 Run the setup chain
`apecx-setup` orchestrates the whole install. Run subcommands individually to verify each, or
`apecx-setup` for the full chain:

```bash
apecx-setup infra       # start docker backends (postgres/redis/minio/ollama/rhea), loopback-bound
apecx-setup llm         # ensure Ollama serving (adopt host OR start apecx-ollama container) + pull model (HTTP /api/pull)
apecx-setup dict        # synonym dictionary (or built lazily on first apecx-mcp start)
apecx-setup rag         # OPTIONAL domain RAG FAISS index (~10 min)
apecx-setup data        # OPTIONAL Globus data transfer (BV-BRC required, VIOLIN optional)
apecx-setup verify      # ✅/❌ health table across all components
apecx-setup capabilities # runnable workflows + primitives + backend roster (one payload)
```

### 2.3 Build the sandbox image (#1c)
Composer "novel Python" steps only execute inside the hardened image:
```bash
docker build -t apecx-novel-sandbox:1.0 \
  src/apecx_integration/composition/steps/_novel_step_container/
```
Executing novel steps requires the explicit opt-in `APECX_T13B_SANDBOX_EXECUTE=1` (fail-closed
otherwise — a CI/test run cannot accidentally run untrusted code).

### 2.4 Configure
| Env var | Purpose |
|---|---|
| `APECX_CONTROL_PLANE_URL` | MCP → control-plane URL (default `http://127.0.0.1:8000`). |
| `APECX_LLM_BASE_URL` | remote OpenAI-compatible LLM; a **local** value keeps Ollama containerized (#7). No remote default. |
| `APECX_LLM_MODEL` | model the synthesis runtime + the ollama readiness probe require. |
| `APECX_T13B_SANDBOX_EXECUTE=1` | enable real novel-step sandbox execution. |
| `APECX_MCP_AUTOSTART_INFRA=0` | probe-only (don't autostart backends). |

---

## 3. Start-up
```bash
apecx-cp serve            # control plane + infra monitor daemon (always-on)
apecx-mcp --locus desktop # MCP server over stdio (desktop client is the orchestrating LLM)
```
On first start the synonym dict builds if absent (10–15 min once). A `RAG DISABLED` banner is
normal when the domain RAG index isn't built.

---

## 4. End-to-end verification

### 4.1 Infrastructure health (three redundant surfaces — use any)
```bash
apecx-setup verify          # ✅/❌ table (dict required; data/ollama/backends optional)
apecx-setup capabilities    # runnable workflows + backend roster
apecx-dashboard --once      # live infra table (CLI view of the monitor)
curl -s http://127.0.0.1:8000/status | jq   # web view JSON: {overall, backends[], recent_failures[]}
# browser: http://127.0.0.1:8000/dashboard  (auto-refreshing HTML)
```
**Expected:** every backend `ready`/`reused`; a genuinely-down backend shows `● ○ ◐` dots and,
if reloadable, is auto-restarted by the monitor daemon (and the restart is recorded to
`~/.apecx/infra_failures.jsonl`). An Ollama with no model pulled correctly reads **DEGRADED**
(not a false "ready").

### 4.2 Per-tool usage checks (MCP surface)
**These are EXECUTED by `tests/e2e_deploy/` — run it, don't hand-verify.** Every call below uses the
parameter names the tool ACTUALLY declares; the earlier draft of this table guessed and got them wrong
(`tests/e2e_deploy/DEPLOYMENT_FINDINGS.md` F2). Derive the live tool count from
`await build_server().list_tools()`, never a hardcoded number (F7).

| Tool | Call (real signature) | Success signal |
|---|---|---|
| `apecx_capabilities` | (no args) | `how_to_run` / `runnable_now` / `backends` present. |
| `list_workflows` | (no args) | the COMPOSABLE catalog (small set incl. `rag_e2e_synthesis_workflow`). **F8: does NOT list promoted tools** like `viral_epitope_analysis`. |
| `describe_workflow` | `{name: "rag_e2e_synthesis_workflow"}` | its schema. **`name`, NOT `workflow_name`.** A non-catalog name → structured `{error:"unknown workflow"}` (F8). |
| `inspect_workflow` | `{name: "viral_epitope_analysis"}` | lightweight/promoted → structured "run it + use `inspect_run`" hint. |
| `run_workflow` | `{name: "viral_epitope_analysis", params: {query: "..."}}` | the report as presentation **TEXT** (NOT a `{status,markdown}` envelope — that's the internal fn; F9). Success = a substantial, on-topic report (G127: assert the VALUE). |
| `harmonized_search` | `{term: "Zika virus", index: "bvbrc_genome"}` | dict-resolved hits. **`term` AND `index`** (index ∈ `bvbrc_genome/…/violin_*/pdb/emdb`); NOT `query`. |
| `compose_workflow` | `{description, user_id}` (no LLM) | no LLM → structured `{error, detail, hint}` (#4). With LLM → a run. |
| `database_statistics` | (with `APECX_DATA_ROOT`) | counts; `{error:...}` (never raises) when unset. |
| `approve_design` | `{token, decided_by}` | fail-closed + scope-bound gate. |
| `infrastructure_status` | (no args) | live backend roster (ollama/postgres/redis/minio/rhea). |

### 4.3 Novel-step sandbox (#1c) — the security boundary
Compose a workflow that emits a **novel step** (bespoke `class:` in the `novel_python` fence),
then run it. Verify:
1. It **loads** (before #1c a novel `class:` made `Workflow.from_config` raise `load_failed`).
2. The step **executes in the sandbox container** and returns its output.
3. **Network egress is blocked** — a novel step that opens a socket fails (`--network none`).
4. A failing novel step surfaces a **traceback** (never a silent empty result).
5. A traversal-y step id is **rejected** at spec validation (no host-write escape).

### 4.4 Ollama-as-container (#7)
```bash
docker ps | grep apecx-ollama                 # managed container running
apecx-setup verify                            # ollama ✅ only when the configured model is present
```
A container that is up but has **no model** reads DEGRADED with an actionable `ollama pull` hint —
`apecx-setup llm` provisions the model via HTTP `/api/pull` (no host `ollama` binary needed).

---

## 5. Automated end-to-end test suite

The verification above is encoded as tests. Gated tests auto-skip when their dependency is
absent, so `make unit` stays green on a bare box; the real checks run when docker/ollama/data
are present.

**The clean-deploy tool/workflow e2e (the executable form of §4) — run it against the INSTALLED
artifact from a fresh venv (no `PYTHONPATH=src`), so it tests the deployed package, not the src tree:**
```bash
# 11 checks: tool surface, real tool calls (correct signatures), + 2 REAL workflow runs to a report.
# Gated: ollama-reachability + dict-present sub-checks auto-skip; the rest always run.
<fresh-venv>/bin/python -m pytest tests/e2e_deploy/ -q
```

```bash
# fast unit suite (no external deps) — must be green
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/ -q

# docker-gated infra + sandbox e2e (real containers)
PYTHONPATH=src .venv/bin/python -m pytest tests/integration/test_infra_monitor_real.py \
    tests/integration/test_novel_step_sandbox_execution.py \
    tests/integration/test_novel_workflow_capstone.py -q

# ollama-gated (live model)
PYTHONPATH=src .venv/bin/python -m pytest tests/integration/test_ollama_model_readiness_against_ollama.py -q

# reproducibility baselines (pinned-LLM; regenerate after composer/expander changes)
APECX_T12_RUN_LIVE_LLM=1 PYTHONPATH=src .venv/bin/python -m pytest tests/reproducibility/ -q
```

Key gated e2e coverage:
- **Infra monitor** (`test_infra_monitor_real.py`): stops a real redis container → the monitor
  detects it (unreachable) → reloads it → records a `FailureEvent`.
- **Sandbox boundary** (`test_novel_step_sandbox_execution.py`): benign novel step runs; network
  egress blocked; a raising step yields a structured traceback.
- **Sandbox capstone** (`test_novel_workflow_capstone.py`): a composed novel workflow loads via
  `Workflow.from_config` **and** its `SandboxedNovelStep` runs the novel code in the real container.
- **Ollama readiness** (`test_ollama_model_readiness_against_ollama.py`): model-present → healthy;
  missing → unhealthy + pull hint; container HTTP `/api/pull` succeeds.

---

## 6. Known-degraded states & follow-ups (honest)
> Full evidence for the items below: `tests/e2e_deploy/DEPLOYMENT_FINDINGS.md`.
- **F8 — discovery surface ≠ tool surface (GATED, UX decision).** `run_workflow("viral_epitope_analysis")`
  works and the flagship is a registered tool, but `list_workflows`/`describe_workflow` (which see only
  the composer catalog) do NOT list it, while `apecx_capabilities` tells the model to "discover names via
  `list_workflows`." A model relying on discovery won't find the flagship. Owner decision needed (unify
  the surfaces, or have `apecx_capabilities` name the promoted tools). Pinned by the e2e harness.
- **F9 — `run_workflow` TOOL returns report TEXT, not a JSON envelope.** A client gets the presentation
  report; the `{status, markdown, run_id}` dict is only on the internal function. Assert on the report
  value, not `status`.
- **F6 — E1/E2 relevance (investigate).** An E1-glycoprotein query returned E2-heavy evidence; may be
  acceptable alphavirus cross-reference or a relevance gap. Not patched; needs a domain call.
- **No LLM configured** — the *desktop* frontier LLM covers the primary analysis path with zero
  apecx LLM config; the backend Ollama is a bounded fallback. `compose_workflow` refuses loudly
  when it genuinely needs an LLM and none is reachable.
- **Data optional** — VIOLIN is optional (Globus-group gated); install completes with a loud
  warning if absent. `harmonized_search` uses the public Globus index anonymously.
- **#1c follow-ups (non-blocking, from the security review):** the sandbox timeout path does not
  yet `docker kill` by name (a hung novel step can orphan a still-capped container — resource
  nuisance, not an escape); `SandboxedNovelStepConfig.sandbox_image` is config-settable but not
  attacker-reachable (the expander never emits it) — pin server-side if a direct-`from_config`
  path is ever added.
- **Reproducibility baselines** for novel-step fixtures need regeneration with the pinned LLM
  after the #1c expander change (they are live-LLM-gated, not in the default suite).
