# Globus end-to-end transfer verification — 2026-05-17

Live verification report per the user directive "Verify that globus file
transfer actually works." Records the exact infrastructure exercised,
what worked, what didn't, and what we learned about the real
data layout.

## TL;DR

**✅ END-TO-END VERIFIED.** All 6 files (VIOLIN + BV-BRC) transferred
from "APECx Data at Argonne LCF" to the operator's local Globus
Connect Personal endpoint via the apecx-mcp-integration code path.

| Layer | Status | Evidence |
|---|---|---|
| Device-code login (`apecx-globus-setup login`) | ✅ | Native-app tokens persisted in globus_sdk's JSONTokenStorage |
| Auth via `build_globus_app(auth_mode='native')` | ✅ | TransferClient + LIST + Transfer all worked silently from persisted tokens |
| Wrapper YAML loads with native-auth env vars | ✅ | `GlobusTransferStepConfig.from_config()` clean |
| LIST source endpoint | ✅ | 6 date-stamped dirs enumerated |
| Submit Transfer task | ✅ | Task UUID `4c6b75ba-51a5-11f1-a4bf-0afffe4617ab` |
| All 6 files on local disk | ✅ | 5 VIOLIN CSVs + 1 BV-BRC CSV (1.5+ GB) |

## Infrastructure exercised

| Component | Value |
|---|---|
| Source endpoint | `8d2e71d6-7a29-41d9-94e5-38d8a95fa5db` (APECx Data at Argonne LCF) |
| Destination endpoint | `e10c104e-4fd4-11f1-91ec-02535127e3d7` (operator's GCP) |
| Native client_id | `8ef2597d-1e4c-43d5-8216-8bac27d727d8` (apecx-mcp-integration native app) |
| Globus user | `onarykov@globusid.org` |
| Auth mode | `native` (device-code + persisted tokens) |
| globus_sdk | 4.5.0 |
| Transfer scope | `urn:globus:auth:scope:transfer.api.globus.org:all` |
| Sync level | `checksum` |
| Verify checksum | `true` |

## Real source layout (discovered live)

The apecx-joshi-anl-general path is **NOT** flat CSVs as the pre-G91
items builder assumed. The actual layout:

```
/apecx-joshi-anl-general/
├── 2024_12_17_VIOLIN/                ← VIOLIN curated CSVs
│   ├── Vaccine_Information.csv               (1.6 MB)
│   ├── Pathogen_Information.csv              (385 KB)
│   ├── Gene_Information.csv                  (496 KB)
│   ├── Vaccine_Pathogen_Information.csv      (100 KB)
│   ├── Gene_Vaccine_Pathogen_Information.csv (76 KB)
│   └── 2025_04_28_VIOLIN_Web_Portal_Data_UML.{html,png}  ← skipped
├── 2025_05_05_BVBRC/                 ← BV-BRC dump
│   ├── BVBRC_epitope__epitopes.csv
│   ├── BVBRC_genome.csv                      (~1.5 GB, ALL genomes)
│   ├── BVBRC_genome_feature.csv
│   ├── BVBRC_protein_feature__domains_and_motifs.csv
│   └── BVBRC_protein_structure__protein_structures.csv
├── 2025_06_25_ProtaBank/             ← not yet wired into apecx
├── 2025_11_05_PubMed/                ← not yet wired into apecx
├── 2025_11_17_IEDB/                  ← not yet wired into apecx
└── 2025_11_18_PDB/                   ← not yet wired into apecx
```

The G91 `build_transfer_items` maps the discovered source layout to
the legacy flat dest layout downstream code expects. Operators
pulling from a newer snapshot can override the date-stamped dir
names via `APECX_GLOBUS_VIOLIN_DIR` / `APECX_GLOBUS_BVBRC_DIR` env
vars.

## ⚠️ BV-BRC content divergence

| | Legacy (gh release) | Modern (Globus source) |
|---|---|---|
| Filename | `BVBRC_genome_alphavirus.csv` | `BVBRC_genome.csv` |
| Size | ~MB-scale | ~1.5 GB |
| Content | Alphavirus-curated subset | ALL BV-BRC genomes |

G91 renames source `BVBRC_genome.csv` → dest
`BVBRC_genome_alphavirus.csv` for filename-only backwards
compatibility (downstream code that opens
`BVBRC_genome_alphavirus.csv` keeps working). The **content** is
broader.

Operators needing alphavirus-only data should:
* Post-filter in pandas: `df[df['Genus'] == 'Alphavirus']`, OR
* Use `apecx-setup --prefer-gh-release` to fetch the curated subset
  from the gh-release tarball.

## Bugs surfaced + fixed (in G91 commit)

1. **Date-stamped source layout** — items builder assumed flat
   `violin/*.csv` paths; actual layout is `2024_12_17_VIOLIN/*.csv`.
2. **Token-storage app_name mismatch** — `apecx-globus-setup login`
   stored tokens under `app_name="apecx-mcp-integration"` but
   nanobrain's GlobusTransferStep used `app_name="nanobrain-globus-transfer-step"`.
   Tokens written by login were invisible to the step.
3. **Nested env-var interpolation unsupported** — wrapper YAML's
   `${A:-${B:-}}` shape failed to parse (the inner `}` survived
   literally). Apecx-side `_resolve_auth_env` now populates
   `APECX_GLOBUS_RESOLVED_*` env vars before YAML load.
4. **TransferData 4.x API change** — pre-4.x 4-arg form
   `TransferData(transfer_client, src, dst, ...)` retired in
   globus_sdk 4.5.0; new signature is `TransferData(src, dst, *, ...)`.
5. **Local mkdir failure with `/~/`** — `Path("/~/dir").parent.mkdir()`
   tries to mkdir `/` (read-only on macOS); now skipped when dest
   uses `/~/` shorthand.
6. **poll_timeout too short for BV-BRC** — bumped wrapper YAML default
   from 600s → 1800s for the ~1.5 GB BV-BRC file.

## Operator recipe (verified working)

```bash
# 1. Register a native Globus app (one-time, 30 sec)
#    https://app.globus.org/settings/developers
#    "Register a thick client or script..."
#    Copy the client_id UUID

# 2. Log in (interactive, browser-based)
PYTHONPATH=src .venv/bin/python -m apecx_integration.cli.globus_setup \
  login --client-id <YOUR_NATIVE_CLIENT_ID>
# → opens URL, you authenticate, paste back code
# → tokens persisted at globus_sdk's default JSON storage

# 3. Set env vars + run the transfer
APECX_GLOBUS_SOURCE_ENDPOINT_ID=<SOURCE_UUID> \
APECX_GLOBUS_DEST_ENDPOINT_ID=<YOUR_GCP_UUID> \
APECX_GLOBUS_NATIVE_CLIENT_ID=<YOUR_NATIVE_CLIENT_ID> \
PYTHONPATH=src .venv/bin/python -m apecx_integration.cli.globus_setup \
  test-transfer
# → LIST source, transfer one CSV as smoke
# OR via the production install path:
apecx-setup data
# → drives build_transfer_items + attempt_globus_data_transfer
# → all 6 files land on local disk under data_dir/
```

## What's NOT verified (deferred)

* **Confidential-client auth path**: code paths exist + are tested
  unit-side. No live confidential client registered yet.
* **`apecx-setup --prefer-gh-release`**: alternative path still
  works (verified pre-G91); not re-exercised today.
* **Production deployment (Aurora HPC)**: uses confidential-client
  auth; this verification was on a developer macOS host with GCP.

## Files involved

| Concern | File |
|---|---|
| Source layout + items builder | `src/apecx_integration/cli/_globus_data_transfer.py` |
| Wrapper YAML | `configs/globus_transfers/violin_bvbrc_transfer_step.yml` |
| `apecx-globus-setup login` | `src/apecx_integration/cli/globus_setup.py` |
| `apecx-globus-setup test-transfer` | `src/apecx_integration/cli/globus_setup.py` |
| `apecx-setup data` integration | `src/apecx_integration/cli/setup.py:_step_data` |
| nanobrain GlobusTransferStep | `nanobrain/library/steps/globus_transfer_step.py` |
| Auth helper | `nanobrain/core/distributed/globus_auth.py` |
| Unit tests | `tests/unit/test_globus_data_transfer.py` (16) + `tests/unit/test_globus_setup_cli.py` (18) |
