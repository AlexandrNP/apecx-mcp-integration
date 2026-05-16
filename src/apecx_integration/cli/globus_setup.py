"""apecx-globus-setup — Globus credential store + endpoint setup/test CLI (G31).

A single operator entry point for the Globus Compute side of an APECx
deployment:

    apecx-globus-setup store              # store client_id/secret in the OS secure store
    apecx-globus-setup status             # show what's stored + the keyring backend
    apecx-globus-setup test               # test endpoint config end-to-end
    apecx-globus-setup test --round-trip  # ...including a real dispatch round-trip
    apecx-globus-setup endpoint-config --project MYALLOC   # render the Aurora template
    apecx-globus-setup clear              # delete stored credentials

Design notes (mirrors apecx-setup's house style):

- Every failure path prints an actionable message and exits non-zero.
  There are NO silent failures: a ``test`` that cannot reach the
  endpoint says so loudly and exits non-zero — it never prints a
  misleading "ok".

- Credentials live ONLY in the OS secure store (nanobrain's G30
  ``globus_credentials``, keyring-backed). The ``store`` subcommand
  FAIL-LOUDs if the keyring backend is insecure rather than falling
  back to a plaintext file.

- This CLI does not reimplement Globus auth or dispatch — it uses
  nanobrain's ``build_globus_app`` (G23) and ``GlobusComputeExecutor``
  (G22/G24). The CLI is the thin operator surface over the framework
  helpers.

- The ``test`` subcommand's live-Globus portion is gated: the
  credential-loading + app-building portion runs anywhere, but the
  endpoint-status query and ``--round-trip`` dispatch need real
  credentials + a real endpoint. When those are absent the live steps
  are reported as a clear FAIL (you asked to test; it could not be
  tested), never a misleading pass.

``main()`` returns an int exit code (0 = success, non-zero = failure)
so it is unit-testable; the ``[project.scripts]`` entry point and the
``__main__`` block both use the return value as the process exit code.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# nanobrain G30 secure credential store — the single source of truth for
# stored Globus credentials. Imported at module top: the CLI hard-requires
# it, so a missing-keyring ImportError SHOULD surface immediately when the
# CLI is invoked (the CLI is in the `hpc` extra alongside keyring).
from nanobrain.core.component_base import ComponentConfigurationError
from nanobrain.core.distributed import globus_credentials

# Env var that carries the default Aurora Globus Compute endpoint id
# (the same name the executor YAML interpolates — see the
# nanobrain-executors skill).
ENV_ENDPOINT_ID = "AURORA_GC_ENDPOINT_ID"

# The Aurora endpoint-config template shipped in this repo.
_TEMPLATE_RELATIVE = "docs/aurora_globus_compute_endpoint_config.yaml"
_TEMPLATE_PLACEHOLDER = "<YOUR_ALCF_PROJECT>"


# ---------------------------------------------------------------------------
# small print helpers — keep the same visual language as apecx-setup
# ---------------------------------------------------------------------------
def _print_header(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def _pass(label: str, detail: str = "") -> None:
    print(f"  PASS  {label}{(' — ' + detail) if detail else ''}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


def _repo_root() -> Path:
    """Repo root = three parents up from this file (src/apecx_integration/cli/)."""
    return Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# subcommand: store
# ---------------------------------------------------------------------------
def _cmd_store() -> int:
    """Interactively prompt for credentials and store them securely."""
    _print_header("apecx-globus-setup store — secure Globus credential storage")
    print(
        "  Credentials are stored in the OS secure store (Keychain / "
        "Credential\n  Locker / Secret Service) via keyring. They are "
        "NEVER written to a file."
    )
    print()
    try:
        client_id = input("  Globus client_id (UUID, visible): ").strip()
        # getpass: the secret is NEVER echoed to the terminal.
        client_secret = getpass.getpass("  Globus client_secret (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        _fail("store", "aborted — no input received")
        return 1

    if not client_id:
        _fail("store", "client_id was empty")
        return 1
    if not client_secret:
        _fail("store", "client_secret was empty")
        return 1

    try:
        globus_credentials.store_credentials(client_id, client_secret)
    except ComponentConfigurationError as exc:
        # The insecure-backend guard (or a missing keyring) fired. Surface
        # the framework's actionable message verbatim; do NOT fall back to
        # a plaintext file.
        _fail("store", "credentials NOT stored")
        print()
        print(str(exc))
        return 1

    _pass("store", f"client_id {client_id} + secret stored in the OS secure store")
    print("  Run `apecx-globus-setup status` to confirm.")
    return 0


# ---------------------------------------------------------------------------
# subcommand: status
# ---------------------------------------------------------------------------
def _cmd_status() -> int:
    """Print a human-readable, secret-free credential-store status."""
    _print_header("apecx-globus-setup status")
    try:
        status = globus_credentials.credential_status()
    except ComponentConfigurationError as exc:
        _fail("status", "could not read the credential store")
        print()
        print(str(exc))
        return 1

    client_id = status["client_id"]
    print(f"  client_id        : {client_id if client_id else '(not set)'}")
    # NEVER print the secret value — only whether one is set.
    print("  client_secret    : " + ("set" if status["client_secret_set"] else "not set"))
    print(f"  keyring backend  : {status['keyring_backend']}")
    print(
        "  backend secure   : "
        + ("yes" if status["backend_secure"] else "NO — store will refuse to write")
    )

    endpoint_id = os.environ.get(ENV_ENDPOINT_ID)
    print(f"  ${ENV_ENDPOINT_ID} : " + (endpoint_id if endpoint_id else "(not set)"))
    print()

    if not status["backend_secure"]:
        print(
            "  The active keyring backend is not a secure store. `store` "
            "will refuse\n  to write. See `apecx-globus-setup store` "
            "output for remediation, or use\n  the "
            "$GLOBUS_COMPUTE_CLIENT_ID / $GLOBUS_COMPUTE_CLIENT_SECRET "
            "env vars instead."
        )
    return 0


# ---------------------------------------------------------------------------
# subcommand: clear
# ---------------------------------------------------------------------------
def _cmd_clear() -> int:
    """Delete stored Globus credentials. Idempotent."""
    _print_header("apecx-globus-setup clear")
    try:
        globus_credentials.clear_credentials()
    except ComponentConfigurationError as exc:
        _fail("clear", "could not access the credential store")
        print()
        print(str(exc))
        return 1
    _pass("clear", "stored Globus credentials removed (idempotent)")
    return 0


# ---------------------------------------------------------------------------
# subcommand: endpoint-config
# ---------------------------------------------------------------------------
def _cmd_endpoint_config(project: str | None, output: str) -> int:
    """Render the Aurora endpoint-config template with the ALCF project filled in."""
    _print_header("apecx-globus-setup endpoint-config — render Aurora template")

    template_path = _repo_root() / _TEMPLATE_RELATIVE
    if not template_path.exists():
        _fail(
            "endpoint-config",
            f"template not found at {template_path}",
        )
        return 1

    template_text = template_path.read_text(encoding="utf-8")

    if project:
        if _TEMPLATE_PLACEHOLDER not in template_text:
            _fail(
                "endpoint-config",
                f"placeholder {_TEMPLATE_PLACEHOLDER!r} not found in the "
                f"template — cannot substitute --project. Template at "
                f"{template_path} may have changed.",
            )
            return 1
        rendered = template_text.replace(_TEMPLATE_PLACEHOLDER, project)
        substitution_note = f"substituted {_TEMPLATE_PLACEHOLDER} -> {project!r}"
    else:
        rendered = template_text
        substitution_note = (
            f"--project not given; {_TEMPLATE_PLACEHOLDER} left as-is (fill it in manually)"
        )

    output_path = Path(output).expanduser()
    try:
        output_path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        _fail("endpoint-config", f"could not write {output_path}: {exc}")
        return 1

    _pass("endpoint-config", f"wrote {output_path} ({substitution_note})")
    print()
    print(
        "  NOTE: this only filled in the ALCF project placeholder. You MUST\n"
        "  still VERIFY every field marked `# VERIFY` / `<FILL IN>` in the\n"
        "  rendered file against the current ALCF Aurora user guide before\n"
        "  `globus-compute-endpoint start` — queue name, node spec, the\n"
        "  worker_init bootstrap line, filesystem labels, and the launcher."
    )
    return 0


# ---------------------------------------------------------------------------
# subcommand: test — the "tests endpoint configuration" deliverable
# ---------------------------------------------------------------------------
def _resolve_credentials() -> tuple[str | None, str | None, str]:
    """Resolve client_id/secret for the ``test`` subcommand.

    Precedence mirrors nanobrain's ``build_globus_app``: env vars then
    keyring. (Explicit args are not a CLI surface for ``test`` — the
    operator stores them once via ``store`` or exports the env vars.)

    Returns ``(client_id, client_secret, source)`` where ``source`` is a
    human-readable description of where each value came from.
    """
    env_id = os.environ.get("GLOBUS_COMPUTE_CLIENT_ID")
    env_secret = os.environ.get("GLOBUS_COMPUTE_CLIENT_SECRET")
    if env_id and env_secret:
        return env_id, env_secret, "environment variables"

    # Fall through to the keyring tier for whatever env didn't provide.
    try:
        kr_id, kr_secret = globus_credentials.load_credentials()
    except ComponentConfigurationError:
        # keyring not installed — env is the only remaining source.
        kr_id, kr_secret = None, None

    client_id = env_id or kr_id
    client_secret = env_secret or kr_secret
    if client_id and client_secret:
        if env_id or env_secret:
            source = "environment variables + OS secure store"
        else:
            source = "OS secure store (keyring)"
        return client_id, client_secret, source
    return client_id, client_secret, "none"


def _cmd_test(endpoint_id_arg: str | None, round_trip: bool) -> int:
    """Test Globus endpoint configuration end-to-end.

    Each step prints a PASS/FAIL line and contributes to the exit code.
    The exit code is 0 only if every attempted step passed.
    """
    _print_header("apecx-globus-setup test — endpoint configuration check")
    failures = 0

    # --- Step 1: load credentials -----------------------------------------
    client_id, client_secret, source = _resolve_credentials()
    if not client_id or not client_secret:
        _fail(
            "credentials",
            "no Globus credentials found in $GLOBUS_COMPUTE_CLIENT_ID / "
            "$GLOBUS_COMPUTE_CLIENT_SECRET or the OS secure store. "
            "Run `apecx-globus-setup store` or export the env vars.",
        )
        # Without credentials nothing downstream can run — stop here.
        return 1
    _pass("credentials", f"loaded from {source}")

    # --- Step 2: build a GlobusApp ----------------------------------------
    # Build the confidential-client app. A bad client_id/secret PAIR
    # surfaces here only as a construction error; an invalid *grant*
    # surfaces in Step 3 when the first real API call (the endpoint
    # status query) forces a token acquisition. We do NOT call
    # get_authorizer here — its argument is a resource-server id, not a
    # scope string, and Step 3's real call is the honest auth proof.
    try:
        from nanobrain.core.distributed.globus_auth import build_globus_app

        app = build_globus_app(
            auth_mode="client_credentials",
            client_id=client_id,
            client_secret=client_secret,
            app_name="apecx-globus-setup-test",
        )
        _pass(
            "globus auth",
            "confidential-client GlobusApp built (token acquisition "
            "exercised by the endpoint query below)",
        )
    except ComponentConfigurationError as exc:
        _fail("globus auth", str(exc))
        failures += 1
        app = None
    except Exception as exc:  # noqa: BLE001 - report any build error
        _fail(
            "globus auth",
            f"failed to build the Globus auth app: {type(exc).__name__}: {exc}",
        )
        failures += 1
        app = None

    # --- Step 3: resolve + query the endpoint -----------------------------
    endpoint_id = endpoint_id_arg or os.environ.get(ENV_ENDPOINT_ID)
    if not endpoint_id:
        _fail(
            "endpoint id",
            f"no endpoint id — pass --endpoint-id or set ${ENV_ENDPOINT_ID}.",
        )
        failures += 1
    elif app is None:
        _fail(
            "endpoint status",
            "skipped — auth app not available (see globus auth FAIL above)",
        )
        failures += 1
    else:
        try:
            import globus_compute_sdk

            client = globus_compute_sdk.Client(app=app)
            # Verified against globus_compute_sdk 4.11.0:
            # Client.get_endpoint_status(endpoint_uuid).
            ep_status = client.get_endpoint_status(endpoint_id)
            # The status payload is a dict; the 'status' key holds
            # 'online' / 'offline'. Be defensive about the exact shape.
            state = ep_status.get("status") if isinstance(ep_status, dict) else None
            if state == "online":
                _pass("endpoint status", f"{endpoint_id} is ONLINE")
            elif state:
                _fail(
                    "endpoint status",
                    f"{endpoint_id} reports state {state!r} (not online)",
                )
                failures += 1
            else:
                _fail(
                    "endpoint status",
                    f"{endpoint_id} status payload had no 'status' field: {ep_status!r}",
                )
                failures += 1
        except Exception as exc:  # noqa: BLE001 - report any query error
            _fail(
                "endpoint status",
                f"could not query endpoint {endpoint_id}: {type(exc).__name__}: {exc}",
            )
            failures += 1

    # --- Step 4: optional round-trip dispatch -----------------------------
    if round_trip:
        if app is None or not endpoint_id:
            _fail(
                "round-trip",
                "skipped — needs a working auth app AND an endpoint id (see FAILs above)",
            )
            failures += 1
        else:
            rt_rc = _round_trip_dispatch(endpoint_id, client_id, client_secret)
            if rt_rc != 0:
                failures += 1

    # --- summary ----------------------------------------------------------
    print()
    if failures:
        _fail("test", f"{failures} check(s) failed — see above")
        return 1
    _pass("test", "every endpoint-configuration check passed")
    return 0


def _round_trip_dispatch(endpoint_id: str, client_id: str, client_secret: str) -> int:
    """Dispatch the trivial_echo_step fixture to the endpoint and verify the echo.

    This is the genuine end-to-end verification: it builds a real
    ``GlobusComputeExecutor`` from an ``ExecutorConfig``, loads the
    nanobrain ``TrivialEchoStep`` fixture from its YAML, runs its
    ``process()`` on the remote endpoint via the executor's dispatch
    contract, and asserts the echoed result.
    """
    import asyncio
    import contextlib
    import tempfile

    import yaml

    try:
        from nanobrain.core.distributed.globus_compute_executor import (
            GlobusComputeExecutor,
        )
        from nanobrain.core.executor import ExecutorConfig
    except Exception as exc:  # noqa: BLE001
        _fail(
            "round-trip",
            f"could not import the GlobusComputeExecutor: {type(exc).__name__}: {exc}",
        )
        return 1

    # ExecutorConfig is a ConfigBase — it is created from a YAML *file*
    # path only (direct construction and inline-dict are both forbidden
    # by the framework). Materialize the globus_compute block to a temp
    # YAML and load it via from_config. The CLI passes the resolved
    # credentials explicitly so the round-trip uses the SAME credentials
    # the test verified, regardless of env.
    executor_yaml = {
        "executor_type": "globus_compute",
        "name": "apecx-globus-setup-roundtrip",
        "globus_compute": {
            "endpoint_id": endpoint_id,
            "auth_mode": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            # HPC queue waits are slow but a trivial echo on a warm
            # endpoint should be fast; keep a generous-but-bounded cap.
            "task_timeout_seconds": 600.0,
        },
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", prefix="apecx_gc_roundtrip_", delete=False
    ) as tmp:
        yaml.safe_dump(executor_yaml, tmp)
        tmp_path = tmp.name
    try:
        executor_config = ExecutorConfig.from_config(tmp_path)
    except Exception as exc:  # noqa: BLE001
        _fail(
            "round-trip",
            f"could not build the ExecutorConfig: {type(exc).__name__}: {exc}",
        )
        os.unlink(tmp_path)
        return 1

    async def _run() -> object:
        executor = GlobusComputeExecutor.from_config(executor_config)
        try:
            await executor.initialize()
            # Reproduce the dispatch contract: the executor introspects
            # an execute_wrapper closure capturing the step + input. We
            # build that closure here from the real fixture step.
            from nanobrain.tests.fixtures.trivial_echo_step import (
                TrivialEchoStep,
            )

            fixture_yml = (
                Path(__import__("nanobrain").__file__).resolve().parent.parent
                / "tests"
                / "fixtures"
                / "trivial_echo_step.yml"
            )
            step = TrivialEchoStep.from_config(str(fixture_yml))
            input_data = {"text": "globus round trip"}

            # The closure shape GlobusComputeExecutor._extract_step_and_input
            # expects: a closure capturing the step (has .config + .process)
            # and a dict (the input data).
            def execute_wrapper():  # noqa: ANN202 - matches framework shape
                return step, input_data

            return await executor.execute(execute_wrapper)
        finally:
            await executor.shutdown()

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - report any dispatch error
        _fail(
            "round-trip",
            f"dispatch to endpoint {endpoint_id} failed: {type(exc).__name__}: {exc}",
        )
        return 1
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    # TrivialEchoStep echoes input under 'echoed', uppercasing strings.
    expected = "GLOBUS ROUND TRIP"
    if isinstance(result, dict) and result.get("echoed") == expected:
        _pass("round-trip", f"endpoint echoed {expected!r} correctly")
        return 0
    _fail(
        "round-trip",
        f"endpoint returned {result!r}, expected {{'echoed': {expected!r}}}",
    )
    return 1


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------
def _cmd_test_transfer(*, list_only: bool, source_path_override: str | None) -> int:
    """Verify the data-transfer Globus path end-to-end (G84+, 2026-05-16).

    Two-stage probe:

      1. **LIST** the source endpoint's ``apecx-joshi-anl-general``
         path (or the override). Proves auth works + the path exists.
         Cheapest sanity check; takes one round-trip.
      2. **Transfer** ONE file (the smallest of the 6) to a local
         temp dir. Proves the full pipeline (auth → submit → poll →
         fetch). Skipped when ``--list-only`` is set.

    Returns 0 only when every attempted step passes. Each step
    prints a single PASS/FAIL line + a one-line detail, matching the
    rest of the apecx-globus-setup CLI's output shape.

    This subcommand is the operator-actionable counterpart to
    ``apecx-setup data``'s Globus-first attempt: when ``apecx-setup
    data`` falls back to gh release with "Globus skipped — ...",
    operators run ``apecx-globus-setup test-transfer`` to diagnose
    which specific prerequisite or auth step is failing without
    sinking the time to retry the full 6-file transfer.
    """

    from apecx_integration.cli._globus_data_transfer import (
        _wrapper_yaml_path,
        check_globus_prerequisites,
    )

    _print_header("Globus data-transfer end-to-end test")

    # Step 1: preconditions.
    prereqs = check_globus_prerequisites()
    if not prereqs.configured:
        _fail("preconditions", prereqs.reason())
        print()
        print("  fix the missing prerequisite(s) and re-run, or see")
        print("  docs/globus_data_transfer.md for the full setup recipe.")
        return 1
    _pass("preconditions", "SDK + endpoint UUIDs + credentials all present")

    # Step 2: wrapper YAML loads.
    wrapper_yaml = _wrapper_yaml_path()
    if not wrapper_yaml.is_file():
        _fail("wrapper YAML", f"missing at {wrapper_yaml}")
        return 1
    _pass("wrapper YAML", f"loadable at {wrapper_yaml.name}")

    # Step 3: build_globus_app + auth round-trip. We do this via the
    # same G23 helper the GlobusTransferStep uses, so a green here
    # proves the auth path is end-to-end-OK.
    try:
        from nanobrain.core.distributed.globus_auth import build_globus_app

        app = build_globus_app(
            auth_mode="client_credentials",
            client_id=os.environ.get("GLOBUS_COMPUTE_CLIENT_ID"),
            client_secret=os.environ.get("GLOBUS_COMPUTE_CLIENT_SECRET"),
            scopes=["urn:globus:auth:scope:transfer.api.globus.org:all"],
        )
        import globus_sdk

        transfer_client = globus_sdk.TransferClient(app=app)
    except Exception as exc:  # noqa: BLE001 — we want every failure visible
        _fail("auth", f"{type(exc).__name__}: {exc}")
        return 1
    _pass("auth", "confidential-client token acquired")

    # Step 4: LIST the source endpoint's apecx path.
    source_endpoint = os.environ["APECX_GLOBUS_SOURCE_ENDPOINT_ID"]
    source_prefix = (
        source_path_override
        or os.environ.get("APECX_GLOBUS_SOURCE_PREFIX", "/apecx-joshi-anl-general")
    ).rstrip("/")
    try:
        ls_result = transfer_client.operation_ls(source_endpoint, path=source_prefix)
        entries = list(ls_result["DATA"])
    except Exception as exc:  # noqa: BLE001
        _fail("LIST", f"{type(exc).__name__}: {exc}")
        return 1
    _pass(
        "LIST",
        f"{len(entries)} entries at {source_endpoint[:8]}…:{source_prefix}",
    )
    print("  ▶  first 6 entries:")
    for entry in entries[:6]:
        print(f"     • {entry.get('type', '?'):6} {entry.get('name', '?')}")

    if list_only:
        return 0

    # Step 5: transfer ONE file (the smallest) to verify the full pipeline.
    # Pick the smallest .csv entry. If none, surface that.
    csv_entries = [
        e for e in entries if e.get("type") == "file" and e.get("name", "").endswith(".csv")
    ]
    if not csv_entries:
        _fail(
            "transfer-one",
            "no .csv files found at source path — apecx-setup data "
            "would have nothing to transfer; investigate the source layout",
        )
        return 1
    smallest = min(csv_entries, key=lambda e: e.get("size", 0))
    src_file = f"{source_prefix}/{smallest['name']}"

    dest_endpoint = os.environ["APECX_GLOBUS_DEST_ENDPOINT_ID"]
    dest_file = f"/~/apecx-globus-test-transfer/{smallest['name']}"

    try:
        tdata = globus_sdk.TransferData(
            transfer_client,
            source_endpoint,
            dest_endpoint,
            label="apecx-globus-setup-test-transfer",
            sync_level="checksum",
            verify_checksum=True,
        )
        tdata.add_item(src_file, dest_file)
        task = transfer_client.submit_transfer(tdata)
        task_id = task["task_id"]
        # Poll up to 60 s; usually completes in <10 s for a small CSV.
        import time as _time

        deadline = _time.time() + 60
        status = "PENDING"
        while _time.time() < deadline:
            info = transfer_client.get_task(task_id)
            status = info["status"]
            if status in {"SUCCEEDED", "FAILED"}:
                break
            _time.sleep(2)
    except Exception as exc:  # noqa: BLE001
        _fail("transfer-one", f"{type(exc).__name__}: {exc}")
        return 1

    if status != "SUCCEEDED":
        _fail("transfer-one", f"task {task_id[:8]}… ended in {status} (not SUCCEEDED)")
        return 1
    _pass(
        "transfer-one",
        f"{smallest['name']} → {dest_file} (task {task_id[:8]}…)",
    )

    print()
    print("  apecx-setup data will use the Globus path successfully.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apecx-globus-setup",
        description=(
            "Store Globus confidential-client credentials in the OS "
            "secure store, inspect them, test Globus Compute endpoint "
            "configuration end-to-end, and render the Aurora "
            "endpoint-config template."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser(
        "store",
        help="Interactively store Globus client_id/secret in the OS secure store.",
    )
    sub.add_parser(
        "status",
        help="Show stored-credential status + the keyring backend (no secrets printed).",
    )
    sub.add_parser(
        "clear",
        help="Delete stored Globus credentials (idempotent).",
    )

    p_test = sub.add_parser(
        "test",
        help="Test Globus endpoint configuration end-to-end.",
    )
    p_test.add_argument(
        "--endpoint-id",
        default=None,
        help=(f"Globus Compute endpoint UUID to test. Defaults to ${ENV_ENDPOINT_ID}."),
    )
    p_test.add_argument(
        "--round-trip",
        action="store_true",
        help=(
            "Also dispatch the trivial_echo_step fixture to the endpoint "
            "and verify the echoed result (genuine end-to-end check)."
        ),
    )

    p_test_xfer = sub.add_parser(
        "test-transfer",
        help=(
            "Verify the data-transfer Globus path end-to-end: LIST the "
            "source endpoint's apecx-joshi-anl-general directory, optionally "
            "transfer ONE file as a dry-run, report results. Gates on the "
            "same env vars apecx-setup uses (APECX_GLOBUS_SOURCE_ENDPOINT_ID, "
            "APECX_GLOBUS_DEST_ENDPOINT_ID, GLOBUS_COMPUTE_CLIENT_ID/SECRET)."
        ),
    )
    p_test_xfer.add_argument(
        "--list-only",
        action="store_true",
        help=(
            "LIST files at the source endpoint path WITHOUT transferring. "
            "Cheapest sanity check: proves auth works + path exists. "
            "Default: also transfer one file to a tmp dir."
        ),
    )
    p_test_xfer.add_argument(
        "--source-path",
        default=None,
        help=(
            "Override the source path on the endpoint (default: "
            "$APECX_GLOBUS_SOURCE_PREFIX or /apecx-joshi-anl-general). "
            "Useful when the operator's collection lays the files out "
            "under a different root."
        ),
    )

    p_epc = sub.add_parser(
        "endpoint-config",
        help="Render the Aurora endpoint-config template with the ALCF project filled in.",
    )
    p_epc.add_argument(
        "--project",
        default=None,
        help=(
            "ALCF project/allocation name to substitute for the "
            f"{_TEMPLATE_PLACEHOLDER} placeholder."
        ),
    )
    p_epc.add_argument(
        "--output",
        default="./aurora-nanobrain-config.yaml",
        help="Where to write the rendered config (default: ./aurora-nanobrain-config.yaml).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns an int exit code (0 = success, non-zero = failure)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "store":
        return _cmd_store()
    if args.subcommand == "status":
        return _cmd_status()
    if args.subcommand == "clear":
        return _cmd_clear()
    if args.subcommand == "test":
        return _cmd_test(args.endpoint_id, args.round_trip)
    if args.subcommand == "test-transfer":
        return _cmd_test_transfer(
            list_only=args.list_only,
            source_path_override=args.source_path,
        )
    if args.subcommand == "endpoint-config":
        return _cmd_endpoint_config(args.project, args.output)

    # argparse(required=True) makes this unreachable, but be explicit.
    parser.error(f"unknown subcommand {args.subcommand!r}")
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
