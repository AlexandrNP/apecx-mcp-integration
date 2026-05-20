# Session Checkpoint — Globus-default data acquisition (retire GitHub download)

**Date:** 2026-05-20
**Branch:** `globus-default-data` (worktree `apecx-cowork/wt-globus-default`, off `main` @ `3c3d820`) — **local, unpushed**
**Status:** root-cause auth fix landed (1 commit); the main refactor + live work are pending.
**Purpose:** self-contained handoff so a clean session resumes this task. Read top-to-bottom.

---

## 1. The task

Retire the GitHub-based data download (`gh release download`) and make **Globus file transfer
the sole default** install data-acquisition path. Source: collection **"APECx Data at Argonne
LCF"** (UUID `8d2e71d6-7a29-41d9-94e5-38d8a95fa5db`), path **`/apecx-ramanathan-anl/public`**
(user-confirmed: same collection UUID as the current joshi source, a full data path). Use
**thick-client (native) auth**. This is an **intermediate** step toward sourcing data solely
from the Globus *search* index later.

**User decisions (2026-05-20):** (a) **fully delete** gh — no fallback; (b) proceed code-only
for now, defer live ls/transfer until re-auth.

---

## 2. DONE — offline-auth fix (commit `0cc12bf`)

`apecx-globus-setup login` built `globus_sdk.UserApp(...)` with no config, so globus_sdk's
default `request_refresh_tokens=False` persisted an **online-only** access token (expires ~2
days, no refresh) — the root cause of the live blocker below. Fixed:
`src/apecx_integration/cli/globus_setup.py` now passes
`config=globus_sdk.GlobusAppConfig(request_refresh_tokens=True)`. Regression test:
`tests/unit/test_globus_setup_cli.py::test_login_requests_refresh_tokens` (mocks globus_sdk).
**19 tests pass.** Worktree has the gitignored venv symlink
(`wt-globus-default/.venv -> ../apecx-mcp-integration/.venv`).

---

## 3. BLOCKER — live Globus needs a fresh interactive re-auth

The persisted thick-client tokens at
`~/.globus/app/8ef2597d-1e4c-43d5-8216-8bac27d727d8/nanobrain-globus-transfer-step/tokens.json`
are **access-only and expired** (`has_refresh=False`, both `auth.globus.org` + `transfer.api`
scopes expired `2026-05-18`). No refresh token → no non-interactive renewal → a fresh browser
login is required, which an agent cannot complete. The live `ls` of `/public`, the file-mapping
derivation, and the live transfer test all depend on this.

**Re-auth recipe** (run interactively, e.g. via the `!` prompt prefix; do it AFTER the §2 fix so
the new token is offline/refreshable — the fix is already committed on this branch):
```
PYTHONPATH=src .venv/bin/python -m apecx_integration.cli.globus_setup login \
  --client-id 8ef2597d-1e4c-43d5-8216-8bac27d727d8
```
(open the printed URL, authorize as the Globus user with access to the collection, paste the code).

---

## 4. Remaining work (precise edit points — from the 2026-05-20 audit)

### A. Delete gh entirely + make Globus the sole default
- `cli/setup.py::_step_data` (235–316): remove the gh fallthrough (~303–316); remove the
  `prefer_gh_release` gate (~278) so Globus is the only path.
- `cli/setup.py`: remove the `--prefer-gh-release` flag (~1268–1278) + its threading (~1193, ~1296).
- `cli/setup_data.py`: remove the gh download — `_download_asset` (~53–69), the download body in
  `_run_full_setup` (~492–517), and the gh constants `_ASSET_NAME`/`_DATA_REPO`/`_RELEASE_TAG`
  (~20–22) + the `_gh_available`/`_gh_authenticated` checks.
- **PRESERVE + RELOCATE** (do not delete): the data-dir prompt + `_maybe_update_claude_config`
  (~265) + `_EXPECTED_FILES` (~26–33) + `_DEFAULT_DATA_DIR` (~23). These live only in the gh path
  today; the Globus path (`setup.py:286`) hardcodes `~/.apecx/data` and skips both. **This is the
  "add data dir prompt" directive** — extract the prompt + config-patch into a shared helper and
  call it from the Globus path.

### B. Repoint the source to `/apecx-ramanathan-anl/public`
- `cli/_globus_data_transfer.py`: source prefix `$APECX_GLOBUS_SOURCE_PREFIX` (default
  `/apecx-joshi-anl-general`) → `/apecx-ramanathan-anl/public`. The wrapper YAML
  `configs/globus_transfers/violin_bvbrc_transfer_step.yml` keeps the same
  `source_endpoint_id` (same collection UUID) — only the path changes.
- **BLOCKED PIECE:** the per-file `_DATASET_FILE_MAPPING` (`_globus_data_transfer.py:104–139`) is
  hand-tuned for joshi's dated layout (`2024_12_17_VIOLIN`, `2025_05_05_BVBRC`). The correct
  `/public` mapping needs the **live `ls`** (§3). Until then: repoint the prefix + add a loud
  pre-transfer `operation_ls` existence check (FAIL-LOUD if a mapped source file is absent — never
  silently transfer zero files), and derive the real mapping after re-auth.
- The DEST layout must match where tests + the product expect data: `violin/*.csv` (5 files) +
  top-level `BVBRC_genome_alphavirus.csv` under `APECX_DATA_ROOT` (`_EXPECTED_FILES`,
  `setup_data.py:26–33`). The dest side is known; the source side needs the live listing.

### C. Tests
- `tests/unit/test_setup_data.py` — largely tests the gh path being deleted; rewrite/trim.
- `tests/unit/test_globus_data_transfer.py` — update for the new sole-default + no fallback +
  the new source path; assert the loud existence check.
- **NEW (required, gated):** a real Globus transfer integration test against `/public` (auto-skip
  when creds absent — like the Ollama-gated tests). All current Globus tests are mock-only; per
  the workspace "no mock-only coverage for a component claimed done" rule, this is mandatory
  before calling the default path done. Only real evidence today is the manual
  `docs/globus_transfer_verification_2026-05-17.md`.

### D. Docs
- `docs/globus_data_transfer.md`: native-first recipe (currently only shows the confidential
  `store` recipe — a doc gap), the new source path, the no-gh-fallback reality.
- Repo `CLAUDE.md` "Globus-first data transfer (G82)" section: update to "Globus-only".

---

## 5. Caveats / recommendations (brutal truth)

- **Dest-endpoint hard requirement.** With gh gone, an unset `APECX_GLOBUS_DEST_ENDPOINT_ID`
  (or no Globus Connect Personal installed) is now a HARD install failure — today it silently
  degrades to gh. Convert `check_globus_prerequisites` (`_globus_data_transfer.py:222–252`) from
  silent-skip to a loud, actionable failure ("install GCP + set the dest UUID"). GCP is NOT
  managed by the code — it's a manual operator install.
- **Headless/CI installs break under native auth** (device-code needs a browser). If unattended
  installs must work, retain the confidential-client path for that mode. Don't make native the
  *only* auth.
- **Adoption risk.** Deleting the universal `gh` fallback for a path requiring GCP + auth + a
  personal endpoint raises the install floor sharply with no safety net. User chose full delete;
  this is the trade. (Intermediate-step framing argues for a deprecation window, but the call was
  made — document the new hard prerequisites prominently.)
- **nanobrain `build_globus_app` has the same auth gap.** `nanobrain/core/distributed/globus_auth.py:259–262`
  (native path) builds `UserApp` with no `request_refresh_tokens` config. The §2 fix is apecx-side
  (login persists a refresh token; the transfer step reads it from shared storage). For
  defense-in-depth, also set `request_refresh_tokens=True` in `build_globus_app` — a nanobrain
  change, out of this apecx-`main`-scoped task; flagged as a follow-up.

---

## 6. Setup + ground-truth pointers

- Worktree test cmd: `PYTHONPATH=src .venv/bin/python -m pytest tests/...` (venv symlink in place).
- Commit discipline: pre-run `ruff format` + `ruff check --fix`, then verify `git log -1` advanced.
- Source UUID `8d2e71d6-7a29-41d9-94e5-38d8a95fa5db`; native client_id
  `8ef2597d-1e4c-43d5-8216-8bac27d727d8`; app_name `nanobrain-globus-transfer-step` (MUST match —
  `globus_setup.py:315` + nanobrain `globus_transfer_step.py:294`).
- Key files: `cli/setup.py`, `cli/setup_data.py`, `cli/_globus_data_transfer.py`,
  `cli/globus_setup.py`, `configs/globus_transfers/violin_bvbrc_transfer_step.yml`,
  `docs/globus_data_transfer.md`, `docs/globus_transfer_verification_2026-05-17.md`.
- Throwaway live-`ls` probe (for after re-auth): `/tmp/globus_token_probe.py` (reads tokens.json
  + builds a TransferClient + `operation_ls`).

## 7. Resume order

1. Re-auth (§3) — interactive, only a human can.
2. Live `ls /apecx-ramanathan-anl/public` (recursive) → record the real layout.
3. Derive the `/public` source→dest file mapping (dest must match `_EXPECTED_FILES`).
4. Implement §4A (delete gh + default + prompt-move) and §4B (repoint + loud existence check).
5. Tests §4C incl. the gated live transfer; docs §4D; the dest-endpoint loud-failure caveat.
6. Verify with a real `apecx-setup data` run end-to-end.
