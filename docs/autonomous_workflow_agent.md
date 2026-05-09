# APECx Autonomous Workflow Agent

**Status:** Design / pre-implementation
**Audience:** Orchestrator authors, control-plane engineers, deployment operators, reviewers of multi-session task semantics
**Supplements:** `meta_workflow_orchestration.md`, `agent_workflow_authoring.md`, `hitl_safety_gates.md`, `agent_communication_protocol.md`, `deployment_architecture.md`
**Read first:** `_design_index.md` for the design package context

---

## 1. Purpose and central claim

This document specifies an **autonomous workflow-writing agent**: a long-lived
orchestrator that authors and executes workflows on its own, driven by
schedules / events / queued tasks rather than synchronous user queries, and
that **may** open a back-channel to a human via the MCP client when it wants
input — but does not require one to make progress.

**Central claim:** The autonomous agent is the same code as the interactive
meta-workflow orchestrator (`meta_workflow_orchestration.md`), running in a
different lifecycle and under a different policy configuration. There is no
separate codebase. The differences are:

1. **Trigger model** — `TimerTrigger` / `ManualTrigger` / `EventTrigger`
   instead of (or alongside) the synchronous `start_workflow` MCP entry.
2. **Autonomy mode** — a new `autonomy_level` policy flag controlling whether
   the agent must wait for human approval or may proceed on its own.
3. **Lifecycle** — the agent runs as a separately-deployed service that
   survives client disconnects and operator logouts.
4. **Communication** — when the agent wants human input, it writes a
   "deferred-HITL request" into the existing approvals table; the user
   discovers it via the existing `list_pending_approvals` tool. (No
   server-initiated MCP push in v1; see §11 for v2 deferral.)

The autonomous agent is **not** a new tier in the multi-agent architecture.
It sits inside Tier 1 (orchestrators) with an extended lifecycle.

What this document does **not** do:
- It does not duplicate the workflow-authoring contract (that is in
  `agent_workflow_authoring.md` — the same Strategies A/B/C apply).
- It does not redefine HITL gate semantics (that is in `hitl_safety_gates.md`
  — this doc adds one new policy axis on top).
- It does not specify the long-running framework primitives in detail (those
  are gaps **G21** and **G22** in `nanobrain_capability_gaps.md`).

---

## 2. Use cases (drivers)

### 2.1 Scheduled batch authorship

A scientist team configures: "Every Sunday at 02:00 UTC, take the prior week's
literature digest, author a structural-biology workflow that updates the
target-of-interest cohort, run it, and publish the result to the team's
Slack-mirror channel."

Required behavior: agent triggers on cron; executes Phase 0 → ... → final
artifact; HITL is `pure_autonomous` because the team has pre-approved the
workflow class; cost is bounded by an operator-configured envelope.

### 2.2 Event-driven response

When a new structural cluster is detected by an upstream pipeline (a row
written to the `event_queue` table), the autonomous agent picks up the event,
authors a tournament workflow ranking design candidates against the new
cluster, and surfaces the top-N to the team for review.

Required behavior: trigger on event; execute autonomously through tournament;
**at the HITL gate, the agent stops being autonomous** — it asks the user via
the deferred-HITL channel and waits up to 72h for a decision; on timeout,
applies the gate's default policy.

### 2.3 Long-horizon supervised research

A scientist asks (via Claude Desktop): "Run a multi-week investigation into
all biomarker candidates that meet criteria X. Pause and ask me whenever you
hit an ambiguous classification. Otherwise, keep going. Send me a daily
summary."

Required behavior: trigger from a synchronous `start_autonomous_task` MCP
call; execute through possibly hundreds of workflow runs; ask via deferred-HITL
when uncertainty crosses threshold; produce a daily summary artifact; survive
operator logouts and MCP session restarts.

### 2.4 Off-hours housekeeping

A control-plane operator schedules: "Every night at 03:00, sweep stale runs,
recompute health metrics, refresh any cached UTDs that have aged out, and
escalate anything anomalous to the on-call queue."

Required behavior: schedule trigger; pure-autonomous execution; results
written to existing operator dashboards; no user interaction except for
operator-driven "show me what you did last night" via the new operator MCP
tools (§7).

---

## 3. Autonomy modes — the policy spectrum

Today's design treats HITL as a binary: Strategy B/C plans require an HITL
gate; Strategy A plans run without one. The autonomous agent introduces a
third axis on top of that binary: **how willingly does the agent proceed
without human input.** This axis is independent of authoring strategy.

### 3.1 The three modes

| Mode | Default for | Behavior at every HITL gate | Behavior on uncertainty signal | Cost cap |
|---|---|---|---|---|
| `strict_hitl` | First-time autonomous deployments; regulated data | Hard-block (current default) | Pause; ask via deferred-HITL; default-deny on timeout | Conservative (per-workflow) |
| `opt_in_hitl` | Recurring autonomous tasks with established approval patterns | Hard-block ONLY for hard-gate categories (compliance, capability); other gates auto-approve on timeout per the gate's `timeout-default-approve` policy | Pause; ask via deferred-HITL; **default-approve** on timeout for low-risk classes; default-deny otherwise | Operator-configured per task (§4.4) |
| `pure_autonomous` | Pre-approved batch workflows where every gate is operator-pre-cleared | Hard-block ONLY for compliance + capability; all other gates auto-approve on timeout (timeout window can be configured to 0s for true non-blocking) | Proceed silently; record uncertainty in audit log; do NOT pause | Hard cap; agent halts on cap |

**The mode is a policy choice, not an architectural choice.** The same
orchestrator workflow YAML, with a different `autonomy_level` value at run
start, exhibits all three behaviors.

### 3.2 Mode selection — who decides

The `autonomy_level` is set at task-creation time and frozen for the
lifetime of the task. It cannot be elevated mid-task (e.g., an
`opt_in_hitl` task cannot upgrade to `pure_autonomous` to skip a pending
gate). It can be downgraded mid-task by an operator with the
`autonomy_admin` capability token (a `pure_autonomous` task can be
downgraded to `opt_in_hitl` to require human input on remaining gates).

The hard floor is set per-deployment via the existing capability flag system
(`agent_workflow_authoring.md §2.3`):

| Capability flag | Effect on autonomy |
|---|---|
| `composer.allow_autonomous: false` | All autonomous tasks rejected at trigger time |
| `composer.allow_autonomous: true` + `composer.max_autonomy_level: opt_in_hitl` | Tasks that request `pure_autonomous` are rejected; max effective level is `opt_in_hitl` |
| `composer.allow_autonomous: true` + `composer.max_autonomy_level: pure_autonomous` | All three modes available |

The defaults are conservative: `allow_autonomous: false` and
`max_autonomy_level: strict_hitl` ship as the new-deployment baseline.

### 3.3 The hard-gate carve-out

Three gate categories from `hitl_safety_gates.md` are **never** auto-resolved
regardless of autonomy mode:

| Gate | Why hard-block |
|---|---|
| GATE-A2 (capability pre-check) | A capability the user does not hold cannot be auto-granted; the autonomy mode is not a capability elevation |
| GATE-C1 (compliance / PHI) | Compliance review is a regulatory requirement; the agent does not have a "regulator" role |
| GATE-P2 (output policy when classified `redact`) | Policy classification is the gate; pre-approving is meaningless |

For these gates, even `pure_autonomous` mode pauses indefinitely (the
deferred-HITL request stays open until decided or until the task is cancelled).

---

## 4. Trigger model — what wakes the agent

The autonomous agent is a long-lived workflow whose entry point is **a
trigger, not a `Workflow.run()` call**. Three trigger types are wired into
the meta-workflow orchestrator entry per gap **G22**:

### 4.1 `TimerTrigger` (schedule-driven)

```yaml
# orchestrator.autonomous_schedule.yml — wires the existing meta-workflow to a cron
class: nanobrain.core.workflow.Workflow
config:
  name: scheduled_autonomous_orchestrator
  triggers:
    - class: nanobrain.core.trigger.TimerTrigger
      config:
        cron: "0 2 * * 0"          # Sundays 02:00 UTC
        target: phase0_planning     # the meta-workflow's existing entry step
        payload_factory:
          task_template: weekly_literature_digest
          autonomy_level: pure_autonomous
```

**Where used:** Use cases 2.1, 2.4. The `TimerTrigger` already exists at
`nanobrain/core/trigger.py:1277` (cron + interval); G22 wires it to the
meta-workflow's entry step.

### 4.2 `ManualTrigger` (queued-task-driven)

```yaml
triggers:
  - class: nanobrain.core.trigger.ManualTrigger
    config:
      target: phase0_planning
      activation_source: control_plane.autonomous_task_queue
```

A row written to the `autonomous_task_queue` table (by an operator MCP call
or by an upstream system) wakes the agent. Each row carries the task_id
(§5), the request payload, and the autonomy_level.

**Where used:** Use case 2.3 (operator-initiated long-horizon investigation).

### 4.3 `EventTrigger` (external-event-driven, NEW with G22)

```yaml
triggers:
  - class: nanobrain.core.trigger.EventTrigger
    config:
      target: phase0_planning
      event_source: webhook
      webhook_url: /events/structural_cluster
      event_filter:
        op: contains
        field: cluster.kind
        value: novel
```

An HTTP POST to `/events/structural_cluster` triggers the agent. The optional
`event_filter` reuses the **G1 predicate DSL** (declarative `{op, field, value}`).

**Where used:** Use case 2.2 (event-driven response).

G22 specifies all three trigger primitives in detail. `TimerTrigger` and
`ManualTrigger` exist today; `EventTrigger` is genuinely new.

---

## 5. Multi-session task identity — the `task_id`

A long-lived autonomous task is **not** identified by its triggering MCP
session. It has a session-independent identity: `task_id` (UUIDv7).

| Identity | Lifecycle | Source |
|---|---|---|
| `mcp_session_id` | One MCP client connection (Claude Desktop tab) | MCP protocol |
| `task_id` | One autonomous task (may span days, many MCP sessions) | Generated by control plane on task creation |
| `run_id` | One workflow run within a task (a task may produce many runs) | Generated by control plane per `Workflow.run()` |

The `autonomous_task` row in the control plane carries:

```sql
CREATE TABLE autonomous_task (
    task_id              UUID PRIMARY KEY,
    created_at           TIMESTAMP NOT NULL,
    created_by           TEXT NOT NULL,             -- user_id who initiated
    trigger_kind         TEXT NOT NULL,             -- 'schedule' | 'event' | 'manual'
    autonomy_level       TEXT NOT NULL,             -- 'strict_hitl' | 'opt_in_hitl' | 'pure_autonomous'
    task_template        TEXT NOT NULL,             -- skeleton id or workflow template
    request_payload      JSONB NOT NULL,            -- the original request
    cost_envelope        JSONB NOT NULL,            -- per §4.4
    status               TEXT NOT NULL,             -- 'queued' | 'running' | 'paused' | 'completed' | 'cancelled' | 'failed'
    last_heartbeat_at    TIMESTAMP,
    updated_at           TIMESTAMP NOT NULL,
    UNIQUE (task_id)
);

CREATE TABLE autonomous_task_run (
    run_id               UUID PRIMARY KEY,
    task_id              UUID NOT NULL REFERENCES autonomous_task(task_id),
    started_at           TIMESTAMP NOT NULL,
    finished_at          TIMESTAMP,
    workflow_yaml_hash   TEXT NOT NULL,
    outcome              TEXT,                       -- 'completed' | 'failed' | 'paused_for_hitl' | 'cancelled'
    cost_actual          JSONB
);
```

**Why a separate task identity is necessary:** the existing `Run` row (per
`architecture.md §4`) is sized for a single workflow execution and
strongly tied to an MCP session for status polling. An autonomous task that
spans many runs needs an outer container that survives the inner runs and
that provides the operator-controllable identity (pause / cancel / inspect).

### 5.1 Task lifecycle states

```
queued      → scheduled or event-triggered; not yet executing
  ↓
running     → at least one workflow run is in progress
  ↓ ↑
paused      → a deferred-HITL request is open; waiting on user response
              (NOT the same as a workflow Run being PAUSED — a task can
              hold many runs in different states)
  ↓
completed   → terminal; all required runs finished, all outputs published
cancelled   → terminal; operator cancelled
failed      → terminal; cost cap exceeded, hard-gate default-denied, or
              unhandled framework error
```

State transitions are recorded in the existing provenance graph (G4) and
emit events on the existing `notifications` table (consumed by operator
dashboards).

### 5.2 Heartbeat and dead-task detection

A long-lived autonomous task heartbeats every 60 seconds by updating
`last_heartbeat_at`. A separate watchdog workflow
(`autonomous_task_watchdog.yml`, scheduled every 5 minutes) flags any task
whose heartbeat is >10 minutes stale as `failed: heartbeat_lost` and
notifies the operator. This catches the "the autonomous service crashed
silently" failure mode that would otherwise leave a task in `running`
forever.

---

## 6. Optional HITL via the existing approvals table

**This is the v1 communication design.** It deliberately does not require
server-initiated MCP push (which is gap **G23**, deferred per Option 2).
Instead, it reuses the existing approvals table + polling pattern.

### 6.1 The deferred-HITL request

When the autonomous agent reaches a HITL gate (per `hitl_safety_gates.md §3`)
and `autonomy_level != pure_autonomous` for that gate's category, the agent
writes an **Approval row** to the existing `Approval` table with two new
fields:

```python
class Approval(BaseModel):
    # ... existing fields per hitl_safety_gates.md §6 ...
    task_id:           Optional[str] = None    # NEW — links approval to autonomous task
    deferred_hitl:     bool = False            # NEW — distinguishes "user asked first" from "agent asks user"
    expires_at:        datetime                # existing field; for deferred-HITL, may be much longer (24-72h default)
```

The user discovers the request through the existing `list_pending_approvals`
MCP tool. The polling pattern (Claude Desktop polls every N seconds when
connected) catches the new request. When the user responds via
`approve` / `reject` / `correct`, the autonomous agent's workflow resumes
on its next heartbeat (the agent polls the approvals table for its own
pending requests every 5 seconds).

### 6.2 The 5-second poll loop

The autonomous agent's runtime has a small inner loop:

```python
async def autonomous_loop(task_id: str):
    while not task_terminated(task_id):
        # 1. Check for operator commands (pause/cancel)
        if operator_command_pending(task_id):
            await handle_operator_command(task_id)

        # 2. Check for resolved deferred-HITL requests
        for approval in pending_deferred_hitls(task_id):
            if approval.is_resolved():
                await resume_paused_run(approval.run_id, approval.decision)

        # 3. Drive any unblocked workflow runs forward
        await advance_runnable_runs(task_id)

        # 4. Heartbeat
        await update_heartbeat(task_id)

        # 5. Sleep
        await asyncio.sleep(5)
```

This is intentionally simple. It is NOT a replacement for nanobrain's
`AsyncTriggerExecutor` — it is a thin coordinator above it. Each individual
workflow run still executes through nanobrain's normal trigger cascade.

### 6.3 Why this is sufficient (no MCP push needed in v1)

The user-perceived latency for a deferred-HITL request is bounded by:
- Claude Desktop's polling cadence on `list_pending_approvals` (typically
  configured to 5-30 seconds when connected)
- The agent's own poll loop (5 seconds)

Total worst-case latency: ~30 seconds. For asynchronous human-in-the-loop
decisions (24-72h windows), this is irrelevant.

The cost: when the user is **not** connected to Claude Desktop, they don't
see the request until they next connect. For "fire and forget" use cases
(2.1, 2.4), the user typically reviews on a daily/weekly cadence anyway.
For "interactive supervised research" (2.3), the user is typically present.

**For v2 (gap G23):** add server-initiated MCP push so the user is notified
even when not actively polling. This is deferred because the v1 polling
model serves the use case sufficiently.

---

## 7. Operator controls — new MCP tools

Four new MCP tools surface the autonomous-task lifecycle to the operator
and to Claude Desktop. They sit in the existing MCP surface
(`mcp_surface.md`) alongside the current 23 tools.

### 7.1 `start_autonomous_task`

```python
async def start_autonomous_task(
    task_template: str,                # skeleton ID or workflow template
    user_id: str,
    autonomy_level: str = "strict_hitl",
    cost_envelope: dict = None,        # per §4.4
    request_payload: dict = None,
) -> dict:                              # returns {task_id, status, expires_at}
```

Initiates a new autonomous task (use case 2.3 entry point). Validates
`autonomy_level` against deployment's `composer.max_autonomy_level` flag.
Returns immediately with the task_id; the agent picks the task up on its
next poll (within 5 seconds).

### 7.2 `list_autonomous_tasks`

```python
async def list_autonomous_tasks(
    user_id: str,
    status: Optional[list[str]] = None,    # filter by status; default: all non-terminal
    since: Optional[datetime] = None,
) -> dict:                                  # returns list of task summaries
```

Lists the operator's autonomous tasks. Reads the `autonomous_task` table
directly (no new compute). Returns task_id, autonomy_level, status,
last_heartbeat_at, and an aggregated count of completed/pending/failed
runs per task.

### 7.3 `pause_autonomous_task` / `cancel_autonomous_task`

```python
async def pause_autonomous_task(task_id: str, reason: str) -> dict:
    # Sets a pending operator command; the autonomous loop honors it on next heartbeat
    # In-flight runs are allowed to complete; new runs are not started
    # Pause is recoverable via resume_autonomous_task (sets cmd to 'resume')

async def cancel_autonomous_task(task_id: str, reason: str) -> dict:
    # Hard-cancels: no new runs; in-flight runs are best-effort terminated;
    # task status transitions to 'cancelled'; not recoverable
```

Both operations are recorded in the audit log (G4) with the operator's
user_id and the reason.

### 7.4 `show_autonomous_audit`

```python
async def show_autonomous_audit(
    task_id: str,
    since: Optional[datetime] = None,
    include_redacted: bool = False,    # requires audit_admin capability
) -> dict:                              # returns ordered audit-event list
```

Returns the task's audit trail: every state transition, every workflow run,
every deferred-HITL request and decision, every cost actualization, and
every operator command. Used by the operator to answer "what did the
autonomous agent do last week" — the use case the existing tooling cannot
answer (the closest current tool, `list_pending_approvals`, only shows the
open queue).

These four tools are the **entirety** of the autonomy-specific MCP surface.
All other interaction (workflow inspection via `show_diff`, approval
decisions via `approve`/`reject`/`correct`) reuses the existing tools — a
deferred-HITL request looks like any other approval to the user.

---

## 8. Cost envelope and runaway-autonomy protection

A pure-autonomous task with no cost envelope is a denial-of-service
weapon — it can burn through LLM credits, HPC allocations, and ProxyStore
storage indefinitely. Every autonomous task carries a mandatory cost
envelope:

```yaml
cost_envelope:
  total_llm_tokens:        1_000_000      # hard cap; task fails on exceed
  total_tool_invocations:  500            # hard cap
  total_walltime_minutes:  720            # 12h hard cap
  per_run_llm_tokens:      50_000         # per-workflow-run cap
  per_run_walltime_minutes: 30
  hpc_eligible:            false          # whether the task may submit HPC bundles
  proxystore_quota_mb:     5_000
```

The control plane's accounting layer
(`apecx-mcp-integration/src/apecx_integration/control_plane/accounting/`)
already tracks per-run cost actuals. The autonomous task layer aggregates
across runs and halts the task when any cap is hit.

**On envelope exhaustion:** task transitions to `failed: cost_envelope_exhausted`;
all in-flight runs are best-effort terminated; the operator is notified
through the standard notification channel.

**On near-exhaustion (>80% of any cap):** a deferred-HITL request is filed
asking the operator whether to extend the envelope. This catches the
common case where the operator under-estimated.

The `composer.max_autonomy_level: pure_autonomous` capability requires the
operator to also configure deployment-wide envelope ceilings (per-task and
per-deployment-per-day), so even a misconfigured task cannot exceed
operator policy.

---

## 9. Failure modes

The following failures are specific to autonomous operation. Cross-reference
`hitl_safety_gates.md §16` for the synchronous-orchestrator failure atlas.

| # | Failure | Detection | Recovery |
|---|---|---|---|
| AU-F1 | Autonomous service crashes mid-task | Watchdog (§5.2) flags stale heartbeat after 10 min | Task transitions to `failed: heartbeat_lost`; operator notified; manual `start_autonomous_task` to resume from checkpoint (G5) if desired |
| AU-F2 | Cost envelope exhausted | Accounting layer detects cap exceeded | Task fails with `cost_envelope_exhausted`; near-exhaustion HITL request (§8) often catches this earlier |
| AU-F3 | Hard-gate default-denied (compliance / capability / policy) | Approval timeout fires with default-deny | Task transitions to `failed: hard_gate_denied`; the deferred-HITL request stays in audit log |
| AU-F4 | Operator cancelled mid-run | `cancel_autonomous_task` MCP call | In-flight runs best-effort terminated; task transitions to `cancelled` |
| AU-F5 | Trigger fires but `autonomy_level` exceeds deployment max | Trigger handler validates against `composer.max_autonomy_level` | Trigger rejected; event/schedule retry policy applied; operator notified |
| AU-F6 | Deferred-HITL request expires with no user response (default-deny) | Standard timeout policy (`hitl_safety_gates.md §3`) | Run terminates with `timeout_deny`; task may continue if other runs are independent |
| AU-F7 | Two autonomous tasks race on the same external resource | Resource is operator-managed; race surfaces as a tool-execution error (per G6 escape valve) | First task completes; second's run reports `partial: true` with `errors[]`; task may retry per its policy |
| AU-F8 | Autonomous service rolling-restart drops a task mid-flight | Watchdog detects heartbeat gap; new service instance picks up via task queue | Task resumes from last checkpoint (G5) on the new instance; runs in flight at restart time may need manual re-execution |
| AU-F9 | LLM endpoint unreachable for prolonged period | Per-run failure surfaces as `llm_unreachable`; aggregates to task-level | Task pauses (state: `paused: dependency_unavailable`); operator alerted; automatic resume on dependency recovery if `pause_on_dependency_loss: true` is set |
| AU-F10 | Schedule trigger fires while previous instance still running | Detected by uniqueness check on `(task_template, scheduled_for)` | Default policy: skip the new trigger and emit a warning; alternative `concurrent_runs_allowed: true` lets them run in parallel (operator opt-in) |

---

## 10. Threat surface

Autonomous operation expands the threat surface in two directions worth
naming explicitly. Both are added to `security_threat_model.md` as new
threat entries (T-AU-1, T-AU-2).

### 10.1 T-AU-1 — Runaway autonomy budget exhaustion

**Description.** A misconfigured or compromised autonomous task burns
through compute / LLM credits / HPC allocation faster than the operator
notices. Either (a) the cost envelope was set too high through honest
mis-estimation, (b) the envelope was not set (rejected at task creation
under §8), or (c) an attacker with `composer.max_autonomy_level: pure_autonomous`
elevated capability creates a task with a high envelope.

**Mitigations:**
- Mandatory cost envelope at task creation (§8).
- Per-deployment per-day ceiling enforced at trigger time, not just
  per-task.
- Near-exhaustion deferred-HITL request gives a human a chance to halt
  before full exhaustion.
- Audit log of every operator who creates a high-envelope task is
  reviewable via `show_autonomous_audit`.

### 10.2 T-AU-2 — Deferred-HITL message body as social-engineering vector

**Description.** The deferred-HITL request body (the "message" the user
sees in `list_pending_approvals`) is composed by the autonomous agent.
A compromised prompt template (per `llm_prompt_contracts.md` injection
threat model) or a compromised LLM completion can inject text designed to
trick the user into approving something harmful (e.g., "Click 'approve'
to fix the production outage" when the actual request is to elevate the
agent's capability tokens).

**Mitigations:**
- Every deferred-HITL request is structured (`{gate_id, payload_schema, payload_data}`),
  not free-form. The user-visible rendering is generated by Claude Desktop's
  approval UI from the structured payload, NOT from a free-text message
  the agent emits.
- The audit log records the prompt template's `template_id` + `content_hash`
  used to generate the request, so a post-hoc audit can detect template
  tampering.
- Capability-elevation requests (GATE-A2) are NEVER auto-renderable from
  prompt content — the gate's payload is always a fixed-schema capability
  list, and Claude Desktop renders it from the schema.

---

## 11. What's deferred to v2

The Option 2 pragmatic scope ships the autonomous agent without these
v2-deferred items. Each is a follow-up design exercise.

### 11.1 Server-initiated MCP push (gap G23)

**v1 alternative:** approvals-table polling (§6).

**v2 promise:** when the user is not actively polling Claude Desktop, the
autonomous agent's deferred-HITL request goes unseen until the next user
connect. v2 adds true MCP server push so a connected client receives a
`notifications/message` immediately on request creation.

**Cost of deferral:** ~24h response latency for users who don't habitually
connect to Claude Desktop. Acceptable for v1 use cases; not acceptable for
real-time supervised research at scale.

### 11.2 Cross-task collaboration

**v1:** each autonomous task is independent. Two tasks investigating the
same target do not share evidence; they run independently.

**v2:** task-level evidence sharing through a shared `EvidencePackage`
namespace. Out of scope for v1 because it requires a separate
"collaborative session" abstraction above the per-task `SessionContext`.

### 11.3 Autonomous task templates as a library

**v1:** task templates are referenced by skeleton ID; the library of
autonomous-suitable skeletons is the same as the interactive-orchestrator
skeleton library (`agent_workflow_authoring.md §4`).

**v2:** a curated subset of skeletons is marked `autonomous_safe: true`
in the registry, with declared cost envelopes and pre-approved gate
defaults. Operators can enumerate "what can I run autonomously" without
consulting a human reviewer.

### 11.4 Adaptive autonomy

**v1:** `autonomy_level` is set at task creation and frozen.

**v2:** the agent may downgrade its own autonomy on persistent uncertainty
(e.g., "I have asked the user 3 times in this task; switching to strict_hitl
for the rest"). Out of scope because it requires defining "uncertainty" as
a measurable signal, which is itself a significant design exercise.

---

## 12. Cross-references

| Resource | Location | Used here for |
|---|---|---|
| Master design index | `_design_index.md` | Position of this doc in Cohort 3 |
| Meta-workflow orchestration | `meta_workflow_orchestration.md` | The same workflow code that this doc runs in a different lifecycle |
| Agent workflow authoring | `agent_workflow_authoring.md` | Strategies A/B/C; capability flags (autonomy_level joins this surface) |
| HITL safety gates | `hitl_safety_gates.md` | Gate categories; which gates auto-resolve under which autonomy mode |
| Agent communication protocol | `agent_communication_protocol.md` | A2U pattern (deferred-HITL via approvals table) |
| Deployment architecture | `deployment_architecture.md` | The autonomous-orchestrator as a long-lived service |
| Security threat model | `security_threat_model.md` | T-AU-1, T-AU-2 added |
| Nanobrain capability gaps | `nanobrain_capability_gaps.md` | G21 (detached/long-running), G22 (TimerTrigger/EventTrigger wiring), G23 (deferred to v2) |
| MCP surface | `mcp_surface.md` | Four new operator MCP tools (§7) |
| Implementation task graph | `implementation_task_graph.md` | NB-G21-*, NB-G22-*, MC-AU-*, XT-11 |
| Data layer evolution (precedent) | `data_layer_evolution.md §4` | Lifecycle workflows are the existing autonomous-workflow pattern; this doc generalizes it |
| Workspace policy | `../CLAUDE.md` | Three-attempt rule applies to autonomous failure recovery |

---

## 13. Open questions

1. **Daemon vs. workflow framing.** The autonomous loop in §6.2 looks like
   a daemon — should it be implemented as a nanobrain workflow itself
   (using G18 LoopController + G22 trigger), or as a separately-coded
   service that calls the workflow runtime? **Working hypothesis:**
   workflow itself, because the unification anchor ("everything is a
   nanobrain workflow") wins. But a service-shaped implementation may be
   easier to deploy and observe. **Resolve before MC-AU-01.**

2. **Per-task vs. per-deployment cost envelope.** The cost envelope (§8)
   is per-task. Should there be a separate per-deployment-per-day ceiling
   to catch the case where many tasks individually are within their
   envelopes but collectively exceed deployment budget? **Working
   hypothesis:** yes; track in the existing accounting layer. **Resolve
   before XT-11.**

3. **Operator-cancellation semantics for in-flight LLM calls.** When an
   operator cancels a task whose current workflow run is mid-LLM-call,
   does the LLM call complete (then the run terminates) or is it killed
   immediately? **Working hypothesis:** LLM call completes (typically
   <30s); the run terminates after. Killing mid-call leaks tokens and
   complicates provenance.

4. **Autonomous task vs. interactive task in the same UI.** Claude Desktop
   currently shows pending approvals in one queue regardless of source.
   Should `list_pending_approvals` filter or label deferred-HITL requests
   differently? **Working hypothesis:** add a `source: 'interactive' | 'autonomous'`
   field to the approval payload; the client decides how to render.
   Doesn't change the protocol; small UI hint.

5. **Trigger replay on missed schedules.** If the autonomous service is
   down when a `TimerTrigger` should fire, does the missed schedule
   trigger on service recovery (catch-up) or is it skipped (drift)? Cron
   convention is skip; the "weekly digest" use case wants catch-up. We
   need a per-trigger policy (`on_missed: catch_up | skip | merge`).

6. **The `pure_autonomous` deployment policy.** Is pure-autonomous mode
   ever available outside of operator-controlled deployments, or is it
   permanently locked behind `composer.max_autonomy_level` set by an
   admin? **Working hypothesis:** permanently admin-gated; the regulated-
   data and cost-blast-radius arguments make this the safe default.

7. **Autonomy budget refresh cadence.** Per-day ceilings reset at midnight
   in the operator's tz, midnight UTC, or rolling-24h-window? Calendar
   resets are user-friendly but expose a "midnight burst" race. **Working
   hypothesis:** rolling 24h window.
