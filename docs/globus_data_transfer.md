# Globus-first data transfer (G82, 2026-05-16)

Operator-facing guide for the Globus path that `apecx-setup data`
prefers when configured. Falls back transparently to the legacy
`gh release download` path when Globus isn't set up.

## TL;DR

```bash
# 1. Install Globus Connect Personal (one-time)
#    Download from https://www.globus.org/globus-connect-personal
#    During install, sign in and grab your endpoint UUID from
#    Settings → Endpoints

# 2. Store your confidential-client credentials in the OS keyring
apecx-globus-setup store \
    --client-id  '<your-client-id>' \
    --client-secret '<your-client-secret>'

# 3. Set the source + destination endpoint UUIDs in your shell rc
#    (or in a local .env — see .env.example for the variable names)
export APECX_GLOBUS_SOURCE_ENDPOINT_ID='<source-endpoint-uuid>'
export APECX_GLOBUS_DEST_ENDPOINT_ID='<your-personal-endpoint-uuid>'

# 4. Run the install. Globus is preferred automatically.
apecx-setup
```

## Why Globus

* **Faster** for large transfers from ALCF. The dataset is currently
  small (~1.5 MB of CSVs), but the Globus path also covers future
  larger datasets that the gh-release path can't size up to.
* **More resilient.** Globus tasks queue and retry; gh release
  downloads must complete in one shot.
* **Auditable.** Each transfer produces a Globus task ID that
  `apecx-setup`'s summary table surfaces, so operators can debug
  failed transfers in the Globus web UI.
* **Closer to how production-scale data flows.** The same path
  works for synthetic test data, production VIOLIN updates, and any
  future large dataset additions.

## What gets transferred

Six files from the source collection's `apecx-joshi-anl-general` path:

```
violin/Vaccine_Information.csv
violin/Pathogen_Information.csv
violin/Gene_Information.csv
violin/Vaccine_Pathogen_Information.csv
violin/Gene_Vaccine_Pathogen_Information.csv
BVBRC_genome_alphavirus.csv
```

The source layout matches the `data/violin/` + `data/`
layout that downstream code (VIOLIN entity lookup, BV-BRC
queries) expects.

## Environment variables

Documented in `.env.example`. Required for the Globus path:

| Variable | Purpose |
|---|---|
| `APECX_GLOBUS_SOURCE_ENDPOINT_ID` | Source collection UUID (the "APECx Data at Argonne LCF" collection). Ask the data steward for the production UUID. |
| `APECX_GLOBUS_DEST_ENDPOINT_ID` | Your Globus Connect Personal endpoint UUID (or any collection you can write to). |
| `GLOBUS_COMPUTE_CLIENT_ID` | Confidential client ID. Optional if `apecx-globus-setup store` already wrote it to the keyring. |
| `GLOBUS_COMPUTE_CLIENT_SECRET` | Confidential client secret. Same keyring fallback applies. |

Optional:

| Variable | Default | Purpose |
|---|---|---|
| `APECX_GLOBUS_SOURCE_PREFIX` | `/apecx-joshi-anl-general` | Override the source path prefix if your collection lays the files out under a different root. |
| `APECX_DATA_ROOT` | `~/.apecx/data` | Where local copies of the files land. |

## When the Globus path is skipped

`apecx-setup` decides per run, checking each of:

1. Is `globus_sdk` installed? (Yes if you installed via the `[hpc]`
   extra OR via the full `pip install -e '.[all]'`.)
2. Is `APECX_GLOBUS_SOURCE_ENDPOINT_ID` set in the env?
3. Is `APECX_GLOBUS_DEST_ENDPOINT_ID` set in the env?
4. Are credentials reachable — either both env vars set, OR a
   keyring entry written by `apecx-globus-setup store`?

If any answer is "no", `apecx-setup` prints the reason and falls back
to `gh release download` from the `apecx-data` GitHub release. You
get a working install regardless; the only loss is the speed /
auditability advantage of the Globus path.

You can force the fallback path explicitly with `--prefer-gh-release`,
useful for reproducing pre-G82 installs verbatim or for debugging
the gh path:

```bash
apecx-setup --prefer-gh-release
```

## Diagnosing failures

`apecx-setup`'s summary table tells you which path ran and how it
ended. Possible verdicts for the `data` step:

| Verdict | What happened |
|---|---|
| `ok    data    Globus: transferred 6 items (task_id=...)` | Globus path succeeded. |
| `ok    data    gh release: downloaded + extracted` | gh path succeeded (Globus either skipped or fell back). |
| `fail  data    setup_data exited with code N` | gh release also failed; check the printed stderr. |

When a Globus transfer fails, the output above the summary table
shows the underlying error. The most common ones:

* **`AuthError`**: credentials don't match the source endpoint's
  ACL. Verify with `apecx-globus-setup status`; re-run the
  `apecx-globus-setup store` if your client secret was rotated.
* **`endpoint not active`**: your destination endpoint
  (typically Globus Connect Personal on your laptop) is not
  running. Open Globus Personal Connect and click "Connect".
* **`Task timed out`**: source endpoint queue is full. Re-run;
  Globus will resume the transfer from where it left off
  (sync_level checksum).

## Where the code lives

| Concern | File |
|---|---|
| Wrapper YAML | `configs/globus_transfers/violin_bvbrc_transfer_step.yml` |
| Apecx-side glue | `src/apecx_integration/cli/_globus_data_transfer.py` |
| CLI integration | `src/apecx_integration/cli/setup.py:_step_data` |
| nanobrain primitive | `nanobrain/library/steps/globus_transfer_step.py` (G28) |
| Auth helper | `nanobrain/core/distributed/globus_auth.py` (G23) |
| Credential CLI | `src/apecx_integration/cli/globus_setup.py` (G31) |
| Tests | `tests/unit/test_globus_data_transfer.py` |

## What CAN'T be tested in CI

The end-to-end transfer against the Argonne LCF collection requires
live credentials, a writable destination endpoint, and network
access to ALCF. CI verifies everything UP TO the network round-trip:
wrapper YAML loads, env-var interpolation works, every precondition
branch behaves as documented, the items builder produces the
expected layout. The live path is exercised when an operator runs
`apecx-setup data` with Globus credentials configured.
