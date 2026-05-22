# `apecx-globus-setup` one-shot configuration + extra dirs (2026-05-22)

## What changed

`apecx-globus-setup` now runs with **no arguments** to configure Globus in one
go, and supports **adding extra source directories** via a subcommand.

### `apecx-globus-setup` (no args) — full setup

The subcommand is no longer required. A bare invocation runs `_cmd_setup`:

1. **Web-based (native) login** — the default auth path (thick client, no
   secret). Opens the browser device-code flow via the built-in native
   client_id.
2. **Default source directories — applied silently.** BV-BRC (public) and
   VIOLIN (`apecx-project-all`) are fixed code constants; they never change, so
   setup states it's using them and configures nothing.
3. **Destination endpoint** — the one user-specific value. Resolution order:
   `APECX_GLOBUS_DEST_ENDPOINT_ID` env → persisted config
   (`~/.apecx/globus_config.json`) → **prompt once**, then persist. A new shell
   doesn't re-enter it.

### `apecx-globus-setup add-dir <remote_path> [--dest-subdir NAME]`

Registers an **extra** source directory, fetched **recursively** (everything
under it) at transfer time, landing in `data/<dest_subdir>/` (default
`dest_subdir` = the remote path's last segment). Persisted to
`~/.apecx/globus_config.json`; idempotent on `remote_path`.

## Persistence — `~/.apecx/globus_config.json`

```json
{
  "dest_endpoint_id": "<uuid-or-null>",
  "extra_source_dirs": [
    {"remote_path": "/apecx-ramanathan-anl/foo/bar", "dest_subdir": "bar"}
  ]
}
```

Only **user customization** lives here — the default source dirs stay as code
constants. Unknown keys FAIL-LOUD (pydantic-extra-forbid discipline applied to
JSON; a typo'd key must not be silently dropped). Path overridable via
`$APECX_GLOBUS_CONFIG_PATH` (tests use this).

## How extra dirs flow through transfer

`build_transfer_items` gained an `"extra"` dataset group (in `OPTIONAL_DATASETS`
and the default-all set). When included, each registered dir is appended as a
**recursive** transfer item `{source_path, dest_path, recursive: true}`.
`apecx-setup`'s data step transfers them in the OPTIONAL batch (alongside
VIOLIN), so an extra-dir failure (typo, access gate) warns but never aborts the
required BV-BRC install. The dest endpoint is back-filled from config into the
env before the workflow runs, so the YAML's `${APECX_GLOBUS_DEST_ENDPOINT_ID}`
interpolation resolves.

### nanobrain framework change (recursive transfer)

Recursive directory transfer required a framework change — `GlobusTransferStep`
previously stripped items to `{source_path, dest_path}` and called
`add_item(src, dest)` with no recursive flag, so a directory source would fail.
Now `_coerce_items` preserves an optional `recursive: bool` (default False,
FAIL-LOUD on non-bool) and `add_item(..., recursive=...)` honors it.
`GlobusManifestVerifyStep` already carried the whole item through to
`verified_manifest`, so the flag survives the verify→transfer link unchanged.
nanobrain regression: `tests/unit/test_globus_transfer_step.py`
(`test_coerce_items_preserves_recursive_flag`,
`test_coerce_items_non_bool_recursive_fails_loud`).

## Brutal-truth: what is and isn't verified

**Unit-tested (no network):** the config persistence (11 tests), the CLI
no-arg/add-dir paths with mocked login + input (`test_globus_setup_cli.py`), the
manifest building incl. recursive extra items + dest-endpoint fallback
(`test_globus_data_transfer.py`), and the nanobrain recursive item flag.

**NOT live-verified here** (no browser, no writable dest endpoint):
- the actual browser device-code login the no-arg flow triggers;
- a real recursive directory transfer landing files on disk;
- the end-to-end no-arg → transfer round trip.

These ship unit-tested + (for the transfer leg) covered by the existing gated
live tests (`APECX_GLOBUS_LIVE_TRANSFER=1` + a real dest endpoint). They are
**not** confirmed against live Globus in this environment — stated plainly
rather than implied.

## Honest note on "Globus superseded everything"

Re-stated from the prior doc: Globus's **default** is the web/thick client
(needs a browser); the **thin-client + secret** path is headless and
implemented. The no-arg setup is therefore an **interactive** command (it opens
a browser). Headless/automated configuration still uses the secret path
(`APECX_GLOBUS_AUTH_MODE=client_credentials` + `apecx-globus-setup store`) and
sets endpoints via env, not this interactive flow.
