# Globus data transfer (G82 2026-05-16; G127 sole-path + verify gate 2026-05-21)

Operator-facing guide for the **only** data-acquisition path `apecx-setup`
uses. The legacy `gh release download` fallback was **retired 2026-05-21** —
Globus is now required. If Globus isn't configured and the dataset isn't
already on disk, `apecx-setup data` FAILS LOUD with setup instructions rather
than silently degrading.

## TL;DR (default: web-based login, no secret)

```bash
# 1. Install Globus Connect Personal (one-time) and start it.
#    https://www.globus.org/globus-connect-personal
#    Grab your endpoint UUID from Settings → Endpoints.

# 2. Authenticate via the browser device-code flow (the DEFAULT — thick client,
#    no secret to obtain/store; the token auto-refreshes). A built-in public
#    native-client id is used unless you override $APECX_GLOBUS_NATIVE_CLIENT_ID.
apecx-globus-setup login

# 3. Set the source + destination endpoint UUIDs in your shell rc / .env.
export APECX_GLOBUS_SOURCE_ENDPOINT_ID='<source-collection-uuid>'
export APECX_GLOBUS_DEST_ENDPOINT_ID='<your-personal-endpoint-uuid>'   # REQUIRED

# 4. Run the install. The data step runs the verify→transfer workflow.
apecx-setup
```

## Auth modes — web (default) vs secret (opt-in)

**Native / web (DEFAULT, "thick client").** Interactive browser device-code
login (`apecx-globus-setup login`), no secret. Best for workstations. Tokens
persist with offline-refresh, so you log in once. `apecx-setup` uses this with
no extra config; a built-in public native-client id ships with the tool
(`$APECX_GLOBUS_NATIVE_CLIENT_ID` overrides it).

**Confidential / secret (OPT-IN, "thin client").** Machine-to-machine, no
browser — for **headless installs, CI, automation, HPC**:

```bash
export APECX_GLOBUS_AUTH_MODE=client_credentials
apecx-globus-setup store --client-id '<id>' --client-secret '<secret>'
# or in CI: export GLOBUS_COMPUTE_CLIENT_ID / GLOBUS_COMPUTE_CLIENT_SECRET
```

(Creds are stored under keyring service `nanobrain-globus` — the same place the
transfer step's auth reads from.) The default stays native even when
confidential creds are present; you MUST set `APECX_GLOBUS_AUTH_MODE=client_credentials`
to select the secret path.

**Brutal truth:** the browser device-code flow needs a browser, so a fully
headless/unattended `apecx-setup` MUST use the secret path. Pick native for
laptops, the secret path for servers/CI.

## How it works — verify→transfer workflow (G127)

`apecx-setup data` loads a two-step nanobrain workflow
(`configs/globus_transfers/violin_bvbrc_transfer_workflow.yml`) and drives it
via `Workflow.run`:

```
workflow_input ({items})
    │  DirectLink (auto_transfer)
    ▼
GlobusManifestVerifyStep   ← operation_ls every source path; FAIL-LOUD if any missing
    │  DirectLink (auto_transfer) — carries the validated manifest through
    ▼
GlobusTransferStep         ← submit + poll to SUCCEEDED
    │
    ▼
workflow_output (transfer_status / task_id / items count)
```

The verify step is the gate: it refuses to start a transfer when a source file
is missing (which would otherwise move zero — or a partial subset of — files,
a silent failure). A wrong/unverified source path therefore produces a loud,
file-named error at install time, never a quietly-incomplete dataset.

## What gets transferred

Six files. **VIOLIN and BV-BRC live under different source roots** (re-mapped
2026-05-21):

| Dest (under `APECX_DATA_ROOT`) | Source |
|---|---|
| `violin/Vaccine_Information.csv` | `$VIOLIN_ROOT/Vaccine_Information.csv` |
| `violin/Pathogen_Information.csv` | `$VIOLIN_ROOT/Pathogen_Information.csv` |
| `violin/Gene_Information.csv` | `$VIOLIN_ROOT/Gene_Information.csv` |
| `violin/Vaccine_Pathogen_Information.csv` | `$VIOLIN_ROOT/Vaccine_Pathogen_Information.csv` |
| `violin/Gene_Vaccine_Pathogen_Information.csv` | `$VIOLIN_ROOT/Gene_Vaccine_Pathogen_Information.csv` |
| `BVBRC_genome_alphavirus.csv` | `$BVBRC_ROOT/BVBRC_genome_alphavirus.csv` |

* `$BVBRC_ROOT` defaults to `/apecx-ramanathan-anl/public/data/BV-BRC`
  (**live-verified 2026-05-21**: the 12 MB curated alphavirus file). This
  fixed an earlier content divergence where the source was the ~1.5 GB
  all-genomes file renamed to the alphavirus name at the destination.
* `$VIOLIN_ROOT` defaults to `/apecx-ramanathan-anl/apecx-project-all/violin`
  (**live-verified 2026-05-21**: the 5 VIOLIN CSVs are present). This path is on
  the SAME collection as BV-BRC but is **ACL-gated by the `apecx-project-all`
  Globus Group** — the transfer identity must be a member to read it. (The
  earlier `500 Path not allowed` was that ACL gate, which presents as a path
  restriction on a GCSv5 guest collection; Group membership unlocked the same
  path on the same endpoint.) If your identity is NOT in the Group, the VIOLIN
  verify step fails LOUD with an "add the identity to the Globus Group" hint and
  the install completes on BV-BRC alone (VIOLIN is optional — see below).

## Environment variables

Required for the (now mandatory) Globus path:

| Variable | Purpose |
|---|---|
| `APECX_GLOBUS_SOURCE_ENDPOINT_ID` | Source collection UUID ("APECx Data at Argonne LCF"). Ask the data steward. |
| `APECX_GLOBUS_DEST_ENDPOINT_ID` | **Hard requirement.** Your Globus Connect Personal endpoint UUID (must be running). |
| `GLOBUS_COMPUTE_CLIENT_ID` | Confidential client ID. Optional if `apecx-globus-setup store` wrote it to the keyring. |
| `GLOBUS_COMPUTE_CLIENT_SECRET` | Confidential client secret. Same keyring fallback. |

Optional:

| Variable | Default | Purpose |
|---|---|---|
| `APECX_GLOBUS_VIOLIN_SOURCE_DIR` | `/apecx-ramanathan-anl/apecx-project-all/violin` | Source dir holding the 5 VIOLIN CSVs (ACL-gated by the `apecx-project-all` Group). |
| `APECX_GLOBUS_BVBRC_SOURCE_DIR` | `/apecx-ramanathan-anl/public/data/BV-BRC` | Source dir holding `BVBRC_genome_alphavirus.csv`. |
| `APECX_GLOBUS_AUTH_MODE` | `native` | `native` (browser device-code, DEFAULT) or `client_credentials` (secret/M2M, opt-in for headless/CI). |
| `APECX_GLOBUS_NATIVE_CLIENT_ID` | built-in apecx native app | Override the native-app client_id (public UUID, no secret) used by the browser login. |
| `APECX_DATA_ROOT` | `~/.apecx/data` | Where local copies land. |

## REQUIRED vs OPTIONAL datasets

The dataset is split:

* **BV-BRC — REQUIRED.** On the public path; always reachable with valid creds.
  If the BV-BRC transfer fails, the data step `fail`s.
* **VIOLIN — OPTIONAL.** ACL-gated by the `apecx-project-all` Globus Group.
  **An identity that IS in the Group fetches VIOLIN normally** (the canonical
  data client is — verified 2026-05-21 — so the default install transfers both
  datasets, status `ok`). An identity that is NOT in the Group hits the verify
  gate: `apecx-setup` prints a **loud warning** naming the Group and returns the
  data step as **`partial`** — the install still **COMPLETES successfully**
  (exit 0) on public data. This keeps installs robust across operators
  regardless of their Group membership. VIOLIN-dependent lookups return empty
  until VIOLIN is fetched; re-run `apecx-setup data` once access is granted.
  `apecx-setup verify` reports `violin` as an optional check.

## When Globus is unconfigured

`apecx-setup` checks, in order: `globus_sdk` installed? source endpoint set?
dest endpoint set? credentials reachable (env vars OR the `nanobrain-globus`
keyring entry)? If any is "no":

* If the **required** dataset (BV-BRC) is **already present** locally → the data
  step is `skipped` (nothing to do).
* Otherwise → the data step **FAILS LOUD**, printing exactly which prerequisite
  is missing and how to fix it. There is no `gh` fallback.

**Adoption trade-off (brutal truth):** removing the universal `gh` fallback
raised the install floor — a first-time operator now needs Globus Connect
Personal installed, a personal endpoint UUID, and credentials before they can
get data. That is the deliberate cost of a single, auditable, scalable data
path. The loud failure with copy-paste instructions is the mitigation.

## Diagnosing failures

The `data` step verdict in the summary table:

| Verdict | Meaning |
|---|---|
| `ok      data   Globus: BV-BRC + VIOLIN transferred (task_ids=...)` | Both datasets transferred. |
| `⚠️ partial data  BV-BRC installed; VIOLIN skipped (optional): ...` | BV-BRC (required) succeeded; VIOLIN (optional) failed its verify gate — install COMPLETES (exit 0). Usually the identity isn't in the `apecx-project-all` Group yet. |
| `skipped data  ... already present ...` | Globus unconfigured but the required data is already on disk. |
| `fail  data   Globus required but not configured: ...` | Prereqs missing + no local data. Follow the printed steps. |
| `fail  data   required BV-BRC transfer failed: ...` | The REQUIRED dataset failed (auth, dest endpoint down, task non-SUCCEEDED, or its verify gate found it missing). This is a hard failure. |

Common transfer errors:

* **`AuthError` / credentials**: verify with `apecx-globus-setup status`;
  re-`store` if the secret was rotated. The preflight reads the same
  `nanobrain-globus` keyring entry the transfer uses (fixed 2026-05-21 — these
  used to disagree).
* **`endpoint not active`**: your Globus Connect Personal endpoint isn't
  running. Start it.
* **`Path not allowed` on a source path**: the path is outside the source
  collection's GridFTP path-restriction (see the VIOLIN note above).

## Where the code lives

| Concern | File |
|---|---|
| Workflow (verify→transfer) | `configs/globus_transfers/violin_bvbrc_transfer_workflow.yml` |
| Verify step wrapper | `configs/globus_transfers/violin_bvbrc_verify_step.yml` |
| Transfer step wrapper | `configs/globus_transfers/violin_bvbrc_transfer_step.yml` |
| Apecx-side driver | `src/apecx_integration/cli/_globus_data_transfer.py` |
| CLI integration | `src/apecx_integration/cli/setup.py:_step_data` |
| Verify primitive (nanobrain) | `nanobrain/library/steps/globus_manifest_verify_step.py` (G127) |
| Transfer primitive (nanobrain) | `nanobrain/library/steps/globus_transfer_step.py` (G28) |
| Auth helper (nanobrain) | `nanobrain/core/distributed/globus_auth.py` (G23) |
| Credential CLI | `src/apecx_integration/cli/globus_setup.py` (G31) |
| Unit tests | `tests/unit/test_globus_data_transfer.py` |
| Gated live tests | `tests/integration/test_globus_transfer_live.py` |

## What CAN'T be tested in CI

A full transfer needs live credentials, a writable destination endpoint, and
network access to ALCF. The gated live test
(`tests/integration/test_globus_transfer_live.py`) runs the **missing-source
gate** against real source auth (proving the verify gate blocks + the driver
fails loud), and runs the **full transfer** only when
`APECX_GLOBUS_LIVE_TRANSFER=1` plus a real dest endpoint is set. Unit tests
cover everything up to the network round-trip.
