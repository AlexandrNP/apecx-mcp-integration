# Aurora Globus Compute endpoint — setup runbook

**Date**: 2026-05-14. **Audience**: the operator (you) — the steps that
touch ALCF/Aurora must run on your account; I cannot SSH to Aurora.

**Brutal-truth disclaimer**: the Aurora-specific values in the config
template below (queue names, `cpus_per_node`, `filesystems` directive,
the `worker_init` module stack) are derived from ALCF documentation,
**not from a verified run on Aurora**. ALCF changes queue/partition
details over time. Treat the template as a starting point and verify
every Aurora-specific value against the current ALCF Aurora user guide
before you `start` the endpoint. Where I am uncertain a field is
marked `# VERIFY` or `<FILL IN>`.

This runbook stands up a **Globus Compute** endpoint on an Aurora login
node so the nanobrain `GlobusComputeExecutor` (shipped this session)
can dispatch step execution to Aurora compute nodes.

---

## 0. What you need before starting

| # | Requirement | Why |
|---|---|---|
| 1 | An active ALCF account + an **Aurora allocation** (project name) | The PBS `account:` field |
| 2 | A Globus account (comes with the ALCF identity) | All Globus services |
| 3 | A **Globus confidential client** (client_id + client_secret) | The nanobrain executor's primary auth mode is `client_credentials` (your choice). Register one at <https://app.globus.org/settings/developers> → "Register a service account or application credential". |
| 4 | `nanobrain` (+ the step's package + its deps) installed in a Python env **on Aurora** | The remote worker reconstructs the step via `from_config` and runs `process()` — it must `import nanobrain`. See §4. |
| 5 | The step's config YAML resolvable on Aurora's filesystem | The executor ships a config-file *path*; the worker reads it. Put step configs on `/lus/flare` (see §5 + the Globus Transfer note). |

---

## 1. Register the Globus confidential client

1. Go to <https://app.globus.org/settings/developers>.
2. "Register a service account or application credential for automation".
3. Note the **Client UUID** → this is `GLOBUS_COMPUTE_CLIENT_ID`.
4. Generate a **client secret** → this is `GLOBUS_COMPUTE_CLIENT_SECRET`.
   Store it like any secret (a secrets manager / `.env` not in git).
5. The confidential client must be **permitted to use the endpoint**.
   Globus Compute endpoints have an access policy; add the client's
   identity to the endpoint's allowed-users list (or register the
   endpoint under the same client). This is the step most likely to
   bite you — a `client_credentials` identity that is not on the
   endpoint's allow-list gets an auth error at submit time.

### Store the credentials securely (recommended)

On the **client side** (wherever the nanobrain workflow runs), store
the pair in the OS keychain — do NOT leave secrets in shell history or
plaintext dotfiles:

```bash
apecx-globus-setup store        # interactive; secret prompt is not echoed
apecx-globus-setup status       # confirms what's stored — never prints the secret
```

`apecx-globus-setup store` writes to the OS secure store (macOS
Keychain / Linux Secret Service / Windows Credential Locker) via
`keyring`. If the environment has **no secure backend** (e.g. a
headless Linux box with no Secret Service), `store` **refuses and
FAIL-LOUDs** rather than silently writing plaintext — on such hosts use
the environment-variable fallback below.

The nanobrain Globus auth helper resolves credentials in this order:
explicit config args → environment variables → keyring. So once
stored, you set nothing else.

**Environment-variable fallback** (headless / CI, or if you prefer):

```bash
export GLOBUS_COMPUTE_CLIENT_ID=<client-uuid>
export GLOBUS_COMPUTE_CLIENT_SECRET=<client-secret>
```

These names are read by **both** the nanobrain executor and
`globus_compute_sdk` itself.

---

## 2. Install the endpoint software on an Aurora login node

SSH to an Aurora login node, then — inside a conda/venv you control
(NOT the base env):

```bash
# on an Aurora login node
module load <python-module>          # VERIFY: the current ALCF python module
conda create -n gce python=3.11 -y    # or reuse an existing env
conda activate gce
pip install globus-compute-endpoint   # pulls globus-compute-sdk too
```

---

## 3. Configure the endpoint

```bash
globus-compute-endpoint configure aurora-nanobrain
```

This creates `~/.globus_compute/aurora-nanobrain/config.yaml`. Replace
it with the template below (also shipped as
`docs/aurora_globus_compute_endpoint_config.yaml` — copy that file to
Aurora and edit it there).

The endpoint process itself authenticates with Globus on first
`start`. For an unattended endpoint, run it under the **same
confidential client** — set `GLOBUS_COMPUTE_CLIENT_ID` /
`GLOBUS_COMPUTE_CLIENT_SECRET` in the login-node shell before `start`,
and `globus-compute-endpoint` will use them instead of an interactive
login.

### Config template (PBS Pro provider for Aurora)

```yaml
# ~/.globus_compute/aurora-nanobrain/config.yaml
# Globus Compute endpoint — Aurora (PBS Pro). VERIFY every Aurora-
# specific value against the current ALCF Aurora user guide.
display_name: aurora-nanobrain

engine:
  type: GlobusComputeEngine

  # The Parsl provider that talks to Aurora's PBS Pro scheduler.
  provider:
    type: PBSProProvider

    # --- allocation + queue (ALCF-specific — FILL IN / VERIFY) ---
    account: <YOUR_ALCF_PROJECT>          # the PBS -A allocation
    queue: debug                          # VERIFY: debug | prod | ... per ALCF
    walltime: "01:00:00"

    # --- node shape ---
    cpus_per_node: 208                    # VERIFY against current Aurora node spec
    nodes_per_block: 1
    init_blocks: 0                        # 0 = don't pre-allocate; scale on demand
    min_blocks: 0
    max_blocks: 1                         # raise for more concurrent capacity

    # --- PBS directives ---
    # filesystems=...:flare makes /lus/flare available to the job; home
    # for the conda env. VERIFY the filesystem labels for current Aurora.
    scheduler_options: "#PBS -l filesystems=home:flare"
    select_options: ""                    # VERIFY: any -l select=... extras

    # --- worker bootstrap — THE LOAD-BEARING LINE ---
    # This runs on every compute node before workers start. It MUST
    # leave `python` able to `import nanobrain` (and the step's package
    # + its deps). Adjust to your Aurora env exactly.
    worker_init: >-
      module load <python-module> ;
      conda activate /lus/flare/projects/<YOUR_ALCF_PROJECT>/<you>/envs/gce ;
      export PYTHONPATH=/lus/flare/projects/<YOUR_ALCF_PROJECT>/<you>/nanobrain:$PYTHONPATH

    # --- launcher ---
    launcher:
      type: MpiExecLauncher        # VERIFY: Aurora's recommended launcher

# Endpoint access — restrict who can submit. Add the confidential
# client identity from §1 here (exact key name VERIFY against the
# globus-compute-endpoint version's schema).
# allowed_functions: ...   # optional: pin to specific function UUIDs
```

> **Why `init_blocks: 0`**: the endpoint does not hold a PBS allocation
> idle. The first task submission triggers a `qsub`; the block scales
> back down when idle. Expect a scheduling delay on the first task —
> the executor's `task_timeout_seconds` default is 3600 s for exactly
> this reason.

---

## 4. The Aurora-side Python environment (the real deployment work)

The `GlobusComputeExecutor` uses **approach (B)**: it ships
`(step_config_path, step_class_name, input_data)` to the endpoint, and
the worker does `importlib` → `step_class.from_config(path)` →
`process()`. Therefore the Aurora worker env (the one `worker_init`
activates) **must** have:

- `nanobrain` importable (same major version as the client side);
- the step's own package importable (e.g. `apecx_integration` if you
  dispatch an apecx step; `rhea` if a step lazy-imports it);
- every dependency that step's `process()` touches.

The cleanest path: build the conda env on `/lus/flare` (shared
filesystem, visible to compute nodes), `pip install -e` the same
nanobrain checkout you run on the client. **A version skew between the
client-side and Aurora-side nanobrain is a silent-failure risk** — the
`from_config` reconstruction can succeed against a drifted schema and
produce subtly wrong behavior. Pin both sides to the same commit.

---

## 5. Start the endpoint + capture the UUID

```bash
# in the login-node shell, with GLOBUS_COMPUTE_CLIENT_ID/_SECRET set
globus-compute-endpoint start aurora-nanobrain
globus-compute-endpoint list      # shows the endpoint UUID + status
```

Copy the **endpoint UUID**. On the **client side**, that is what the
nanobrain executor config's `endpoint_id` points at:

```bash
export AURORA_GC_ENDPOINT_ID=<endpoint-uuid>
```

---

## 6. Wire it into a nanobrain workflow

The executor is selected via a workflow's `executors:` block and bound
to a step with `executor: <name>`. Example (this is the shape used by
`composition/workflows/rhea_muscle_alignment_aurora/workflow.yml`):

```yaml
executors:
  aurora:
    executor_type: globus_compute
    globus_compute:
      endpoint_id: "${AURORA_GC_ENDPOINT_ID}"
      auth_mode: client_credentials       # reads $GLOBUS_COMPUTE_CLIENT_ID/_SECRET
      task_timeout_seconds: 3600

steps:
  alignment_report:
    class: "apecx_integration.composition.steps.alignment_report_step.AlignmentReportStep"
    config: "../rhea_muscle_alignment/steps/alignment_report.yml"
    executor: aurora        # this step's process() runs on Aurora
```

The step's config YAML (`alignment_report.yml`) must be resolvable on
Aurora's filesystem at the path the executor ships — see §5. If the
client-side path differs from the Aurora-side path, stage the config
onto `/lus/flare` first (this is exactly what `GlobusTransferStep` is
for — see the data-staging note below).

---

## 7. Data staging — Globus Transfer

`GlobusComputeExecutor` moves *code execution* to Aurora; it does not
move *files*. For a step whose inputs/outputs are large files, stage
them with `GlobusTransferStep` (shipped this session,
`nanobrain.library.steps.globus_transfer_step.GlobusTransferStep`)
before/after the compute step. You need:

- the **source** collection/endpoint UUID (e.g. your laptop's Globus
  Connect Personal endpoint, or a lab server collection);
- the **Aurora** transfer collection UUID — from the ALCF data-transfer
  Globus page you linked (<https://docs.alcf.anl.gov/data-management/data-transfer/using-globus/>),
  the Aurora `/lus/flare` collection;
- both authorized for the same confidential client (§1), with the
  Transfer scope.

A complete Aurora workflow is then:
`GlobusTransferStep (stage in) → compute step on the GlobusComputeExecutor → GlobusTransferStep (stage out)`.

---

## 8. Verify

The primary verification tool is the `apecx-globus-setup test`
subcommand. With credentials stored (§1) and the endpoint UUID known:

```bash
# auth + endpoint-status check
apecx-globus-setup test --endpoint-id $AURORA_GC_ENDPOINT_ID

# the full round-trip: dispatches a trivial nanobrain step THROUGH the
# real GlobusComputeExecutor to the endpoint and checks the result
apecx-globus-setup test --endpoint-id $AURORA_GC_ENDPOINT_ID --round-trip
```

Each step prints `PASS` / `FAIL`; any failure exits non-zero — there
is no misleading "ok". A green `--round-trip` is the real-data
verification the workspace mocks policy requires.

Equivalently, the gated pytest integration test:

```bash
cd apecx-mcp-integration   # or the nanobrain repo
GLOBUS_COMPUTE_ENDPOINT_ID=$AURORA_GC_ENDPOINT_ID \
  PYTHONPATH=. .venv/bin/python -m pytest \
  ../nanobrain/tests/integration/test_globus_compute_executor_local.py -q
```

Until one of these runs green against Aurora, the
`GlobusComputeExecutor` is "built + unit-tested" but **not** "verified
on Aurora" — be honest about that distinction in any status report.

---

## Common failure modes (FAIL-LOUD — the executor surfaces all of these)

| Symptom | Cause | Fix |
|---|---|---|
| `FAIL-FAST: ... 'globus_compute_sdk' ... not installed` | client-side venv missing the SDK | `pip install globus-compute-sdk` |
| `FAIL-FAST: ... requires confidential-client credentials. Missing: ...` | `GLOBUS_COMPUTE_CLIENT_ID/_SECRET` not exported | export them (§1) |
| auth error at submit | confidential client not on the endpoint's allow-list | add the client identity to the endpoint access policy (§1.5) |
| `remote task ... exceeded task_timeout_seconds` | PBS queue wait > timeout, or endpoint not scheduling | raise `task_timeout_seconds`; check `globus-compute-endpoint list` shows the endpoint online |
| remote `ModuleNotFoundError: nanobrain` (in the re-raised traceback) | `worker_init` did not activate an env with nanobrain | fix `worker_init` (§4) |
| remote `FileNotFoundError` on the step config path | the step config YAML is not at that path on Aurora | stage it to `/lus/flare` with `GlobusTransferStep` (§7) |
| step runs but behaves subtly wrong | client/Aurora nanobrain version skew | pin both sides to the same commit (§4) |
