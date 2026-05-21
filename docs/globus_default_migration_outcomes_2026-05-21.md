# Globus-default data migration — outcomes (2026-05-21)

Outcome record for retiring the GitHub-release data download and making the
Globus file transfer the sole, framework-native, fail-loud data-acquisition
path. Branch `globus-default-data` (worktree `wt-globus-default`); nanobrain
work on branch `academy-integration`.

## What shipped

| # | Change | Where |
|---|---|---|
| 1 | **`GlobusManifestVerifyStep`** — new nanobrain `BaseStep`: `operation_ls`-verifies every source path exists before transfer; FAIL-LOUD naming each missing file; passes the manifest through. | `nanobrain/library/steps/globus_manifest_verify_step.py` (G127) |
| 2 | **verify→transfer workflow** — 2-step nanobrain workflow; `DirectLink`s with `auto_transfer: true`; exposes transfer status/task_id/count as workflow outputs. | `configs/globus_transfers/violin_bvbrc_transfer_workflow.yml` (+ two step wrappers) |
| 3 | **Driver rewrite** — loads the workflow and drives `Workflow.run`; success iff `transfer_status=='SUCCEEDED'`; captures `step_failed` events to surface the real error. | `cli/_globus_data_transfer.py` |
| 4 | **Keyring service-name fix** — preflight now delegates to nanobrain `globus_credentials.load_credentials` (service `nanobrain-globus`) instead of a hardcoded `apecx-globus-setup`. | `cli/_globus_data_transfer.py` |
| 5 | **Source re-map** — BV-BRC → `/apecx-ramanathan-anl/public/data/BV-BRC/BVBRC_genome_alphavirus.csv` (verified); VIOLIN → `/apecx-ramanathan-anl/apecx-project-all/` (steward-stated); independent env-overridable roots. | `cli/_globus_data_transfer.py` |
| 6 | **gh removal** — deleted `_download_asset` / `_gh_available` / `_gh_authenticated` / `_run_full_setup` / `--prefer-gh-release`; data step FAILS LOUD when Globus unconfigured + no local data. | `cli/setup.py`, `cli/setup_data.py` |
| 7 | **Prompt + config-patch relocation** — `prompt_for_data_dir` + Claude-config patch now run on the Globus path (previously only on the deleted gh path). | `cli/setup_data.py`, `cli/setup.py` |
| 8 | **Tests + docs** — see below. | `tests/`, `docs/` |
| 9 | **Actionable verify-step error classification** (2026-05-21 follow-up, nanobrain `6c04996`) — a non-404 `operation_ls` failure now emits a remediation hint: 403/no-ACL → "authorization; if Group-gated, add the identity to the Globus Group (admin action)"; 500 "Path not allowed" → "a DIFFERENT collection serves this path"; offline → "start the collection". | `nanobrain/library/steps/globus_manifest_verify_step.py` |

## Live findings (why probing first mattered)

The 2026-05-20 checkpoint's source layout was **stale**. Live `operation_ls`
on 2026-05-21 (M2M client_credentials, creds from keyring `nanobrain-globus`):

* `/apecx-joshi-anl-general` (the old code default) → **403 EndpointPermissionDenied**. The previous mapping was dead for this identity.
* `/apecx-ramanathan-anl/public/data/violin` → **404** (checkpoint claimed 5 CSVs + a .txt here — wrong).
* `/apecx-ramanathan-anl/public/data/BV-BRC/BVBRC_genome_alphavirus.csv` → **present, 12 MB** (the curated alphavirus file).
* `/apecx-ramanathan-anl/apecx-project-all` (where the steward says VIOLIN now lives) → **500 "Path not allowed"** — a GridFTP *collection path-restriction*, distinct from a 403/ACL failure. This UUID's mapped namespace excludes that path.

**Content-divergence fix (highest-value correctness win):** the prior code
mapped `BVBRC_genome.csv` (~1.5 GB, ALL genomes) → `BVBRC_genome_alphavirus.csv`
at the destination. Downstream `apecx_db_integration` reads it as alphavirus-only
— wrong data that looks right. The `/public` source is the genuinely curated
12 MB file, so the rename hack is gone and source content == expected content.

## Silent-failure analysis (the load-bearing design point)

`Workflow.run` does **not** propagate a step exception — it returns
`status: 'completed'` with empty outputs and emits a `step_failed` event. A
driver that trusted that status would report SUCCESS on a transfer that never
ran. The driver therefore:

1. trusts ONLY `transfer_status == 'SUCCEEDED'` (the data signal), and
2. subscribes to step events, surfacing the captured `step_failed` exception
   message (e.g. the exact list of missing source files) on any other outcome.

Verified live: a missing source file → driver returns `status='fail'` naming
the file; the transfer step never runs.

## Multiple workflow-authoring paths explored (per directive)

* **Hand-authored YAML** (primary, fully working, live-tested) — the shipped
  `violin_bvbrc_transfer_workflow.yml` driven by `Workflow.run`.
* **Lightweight `WorkflowBuilder`** — the same verify→transfer topology builds
  programmatically; a parity test asserts the builder encodes both steps + 3
  nested `DirectLink`s. **Limitation found:** `WorkflowBuilder.load()` dumps
  *flat* step entries (`{name, class, ...inline_fields}`) to YAML, and steps
  carrying inline `input_data_units`/`output_data_units` do not materialize
  cleanly through `Workflow.from_config`'s step loader (steps don't register,
  so links fail to resolve). This is an orthogonal builder/loader integration
  gap, not a Globus issue — the parity test asserts the encoded config rather
  than a full `.load()` round-trip, and the YAML path proves runtime behavior.

## Test evidence (project venv)

* nanobrain: `tests/unit/test_globus_manifest_verify_step.py` — **18 passed**
  (mocked `TransferClient`); `tests/integration/test_globus_manifest_verify_live.py`
  — **2 passed** against the real collection.
* apecx: `test_globus_data_transfer.py` + `test_setup_data.py` +
  `test_globus_transfer_live.py` — **50 passed, 1 skipped** (the full-transfer
  test, which needs a real writable dest endpoint).
* Commits: nanobrain `1075cc5` (G127 step); apecx `4737523` (workflow + driver
  + repoint + gh removal + tests).

## Open items / honest gaps

1. **VIOLIN source endpoint (BLOCKER for VIOLIN).** `apecx-project-all` is not
   reachable from the source UUID `8d2e71d6-…` (`Path not allowed`).

   **Diagnosed precisely 2026-05-21 (Group-UUID follow-up).** The steward
   pointed at Globus Group `64da2fea-bd98-11ef-8092-178fd5b923bd` (confirmed via
   the Groups API — its name IS `apecx-project-all`). Findings: that UUID is a
   **Group, not a collection** (`EndpointNotFound` when addressed as an
   endpoint); the public Data collection still `Path not allowed`s the path; the
   "APECx Submission" collection (`a3628510-…`) returns **403 — "No effective
   ACL rules"** for our identity; and crucially `GroupsClient.get_my_groups()`
   returns **(none)** — i.e. the confidential client's service identity is **NOT
   a member** of the `apecx-project-all` Group. Globus gates this data by Group
   membership, so credentials alone cannot reach it.

   **REMEDIATION (admin action — I cannot do this):** a manager of Group
   `apecx-project-all` (`64da2fea-bd98-11ef-8092-178fd5b923bd`) must add the
   service identity **`bbcdba6f-0c71-4fe2-9d6e-72fe95f2d8e7@clients.auth.globus.org`**
   as a member. After membership propagates, re-probe to find the collection
   that serves the VIOLIN data (it is NOT the public collection — likely a guest
   collection the Group has an ACL on, with its own endpoint UUID), then set
   `APECX_GLOBUS_VIOLIN_SOURCE_DIR` (and add `APECX_GLOBUS_VIOLIN_ENDPOINT_ID` if
   it's a different collection than BV-BRC — that per-dataset-endpoint support is
   NOT yet built; deferred deliberately until the real layout is known, to avoid
   guessing the abstraction). BV-BRC is unaffected (verified, on the public
   collection). The verify gate keeps a wrong VIOLIN path fail-loud meanwhile.
2. **Full live transfer not yet run end-to-end** here — no writable dest
   endpoint (Globus Connect Personal) on this machine. The gated test runs it
   when `APECX_GLOBUS_LIVE_TRANSFER=1` + a real dest is set.
3. **nanobrain `build_globus_app` native-path refresh-token gap** (carried from
   the prior checkpoint §5): `globus_auth.py` native `UserApp` doesn't set
   `request_refresh_tokens=True`. Out of scope for this apecx task; flagged as a
   nanobrain follow-up. Does not affect the M2M (client_credentials) default.

## Adoption trade-off (brutal truth)

Deleting the universal `gh` fallback raised the install floor: a first-time
operator now needs Globus Connect Personal + a personal endpoint UUID + creds
before any data lands. That is the deliberate cost of one auditable, scalable
data path. Mitigation: the data step fails loud with copy-paste setup steps
rather than silently degrading, and the verify gate guarantees a misconfigured
source never yields a quietly-incomplete dataset.

## Follow-up #2 (2026-05-21) — VIOLIN made optional + clean-install test fixes

Once the VIOLIN blocker was confirmed admin-side (Group membership pending), the
directive was: work with public data now, say loudly VIOLIN is missing, but let
the whole setup COMPLETE successfully. Plus: fix the collection errors other
users hit on a clean `make unit`.

**VIOLIN optional (apecx `1a7f93f`).** Datasets partitioned into REQUIRED
(BV-BRC, public) and OPTIONAL (VIOLIN, group-gated). `build_transfer_items(..,
datasets={...})` scopes the manifest. `_step_data` transfers REQUIRED
must-succeed, then OPTIONAL warn-on-fail: a VIOLIN failure prints a loud,
actionable warning (names the `apecx-project-all` Group + "re-run once access
granted") and returns the data step as **`partial`** → `_print_summary` treats
partial as exit 0, so the install completes on public data. `_step_verify`
reports `violin` as a separate OPTIONAL check. The nanobrain verify step stays
STRICT — optionality is purely an apecx CLI policy layer, so there's still no
silent zero-file transfer.

**Clean-install pytest collection: 12 errors → 0 (apecx `4536de8`).** All were
import-time COLLECTION errors (not test failures) that aborted the whole run:
- 10× `tests/benchmarks/problems/**/test_code.py` — codegen-candidate eval
  TEMPLATES (a generated candidate is prepended + run in a subprocess by the
  bench sandbox; standalone they assert-fail at import). Excluded via a
  `pytest_ignore_collect` hook in `tests/conftest.py`.
- `test_academy_real_integration.py` — imported the optional `academy` extra at
  module scope (→ ModuleNotFoundError at collection despite the `integration`
  marker, since marker deselection happens after import). Now
  `pytest.importorskip("academy.agent")`.
- `test_harvester_contract.py` — `importorskip`'d the package but then imported
  `Transform`, a symbol the installed version lacks. Guarded the symbol imports
  → module-level skip on API drift.
Verified: 1733 tests collect with 0 errors.

**Brutal truth on VIOLIN-optional:** making a dataset "optional by policy" is a
small silent-failure risk — a genuinely mis-set VIOLIN path now degrades to a
warning instead of a hard stop. Accepted deliberately because (a) VIOLIN access
is known-pending, and (b) the loud warning + the `partial` verdict + the
`apecx-setup verify` `violin` line keep it visible. When the Group grant lands,
VIOLIN simply succeeds and the warning disappears — no code change needed unless
VIOLIN turns out to be on a different collection (then add the deferred
per-dataset-endpoint support).

## Follow-up #3 (2026-05-21) — VIOLIN access live + wired

The `apecx-project-all` Group grant landed. Verified via the Groups API: the
transfer identity `bbcdba6f-…@clients.auth.globus.org` is now a member of both
`apecx-project-all` and `apecx-ramanathan-anl`. Crucially, that membership
unlocked `operation_ls` of `/apecx-ramanathan-anl/apecx-project-all` on the
**same** endpoint `8d2e71d6` — confirming the earlier `500 Path not allowed`
was an **ACL gate** (presents as a path restriction on a GCSv5 guest
collection), NOT a separate collection. **No per-dataset-endpoint support was
needed** — the deferred abstraction stays unbuilt. (Probing-before-building
paid off a second time: a guessed "different collection" abstraction would now
be dead code.)

VIOLIN files live at `/apecx-ramanathan-anl/apecx-project-all/violin/`: the 5
expected CSVs (+ a `VIOLIN_Curated_References.txt` that is present but NOT
transferred — not in `_EXPECTED_FILES`, not read downstream). The only code
change was `_DEFAULT_VIOLIN_SOURCE_DIR` gaining the `/violin` suffix (apecx
`4f792cc`). Live-verified: `GlobusManifestVerifyStep` passes for all 5 VIOLIN
paths against the real collection; new gated test `test_violin_source_accessible`
pins this (source creds only, no dest).

**VIOLIN stays OPTIONAL by policy** even though it's now accessible: the
canonical client (in the Group) fetches both datasets (`ok`); an operator whose
own identity isn't in the Group still gets a clean `partial` install. That is
the robust behavior across diverse operator identities.

**What is NOT yet exercised here:** the full end-to-end file MOVEMENT (verify →
transfer → files on disk) needs a writable dest endpoint (Globus Connect
Personal); none is available in this environment, and writing test data to a
shared production collection would be inappropriate. Accessibility is proven
(verify passes against real data); the transfer leg is covered by nanobrain's
`GlobusTransferStep` tests + the gated `test_full_transfer_succeeds`
(`APECX_GLOBUS_LIVE_TRANSFER=1` + a real dest). Honest status: **VIOLIN is
confirmed READABLE by the client; a full round-trip transfer is unrun here for
lack of a dest endpoint.**
