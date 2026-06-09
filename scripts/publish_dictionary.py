#!/usr/bin/env python3
"""Publish the local synonym dictionary to the APECx Globus collection.

Backend arm of the two-arm pipeline. The user-facing arm consumes the
result via apecx-harvesters' ``apecx-dict-update`` CLI — see
``apecx-harvesters/docs/two_arm_contract.md`` for the file format.

Upload model (corrected 2026-06-08): direct HTTPS PUT to the collection's
``https_server``. NO local Globus endpoint, NO Globus Transfer task,
NO endpoint pairing. The same HTTPS URL that the user-facing arm GETs
from anonymously is the one we PUT to with an
``Authorization: Bearer <token>`` header.

Pipeline:
  1. Read the local dictionary at ``~/.apecx/dictionary/dictionary.sqlite``
     (override via ``--dict-path``).
  2. Validate it loads and parse its embedded BuildManifest.
  3. gzip-compress it to ``<staging>/dictionary-<version>.sqlite.gz``.
  4. Compute sha256.
  5. Emit a ``MANIFEST.json`` next to the gz with the publish-time
     metadata the user-facing bootstrap needs.
  6. (With ``--upload``) Acquire Globus Auth tokens via client_credentials,
     discover the collection's ``https_server`` via Transfer API, then
     PUT both files to ``${https_server}/<dest-path>/`` using urllib.

Authentication (``--upload``):
  Requires ``GLOBUS_COMPUTE_CLIENT_ID`` + ``GLOBUS_COMPUTE_CLIENT_SECRET``
  in env, matching the existing apecx-globus-setup convention. The
  confidential client MUST have prior consent for both the Transfer scope
  AND the collection's HTTPS data-access scope; configure that once at
  client-registration time on https://app.globus.org/settings/developers.

  Native-app auth is NOT supported here. The publish path is by design a
  machine-driven backend operation; if you want interactive publish, use
  the manual ``globus-cli`` commands the script prints when ``--upload``
  is absent.

Usage:
  scripts/publish_dictionary.py --staging-dir /tmp/dict_publish
  scripts/publish_dictionary.py --staging-dir /tmp/dict_publish --upload
  scripts/publish_dictionary.py --dict-path /path/to/dict.sqlite --staging-dir /tmp/p
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# The APECx Data at Argonne LCF collection UUID. Confirmed by user
# 2026-06-08. Stable across builds; reused for both Transfer API
# scoping AND the collection HTTPS scope.
APECX_COLLECTION_UUID = "8d2e71d6-7a29-41d9-94e5-38d8a95fa5db"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_local_manifest(dict_path: Path) -> dict:
    """Pull the BuildManifest JSON out of the local dict's manifest table."""
    import sqlite3

    conn = sqlite3.connect(f"file:{dict_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM manifest WHERE key = ?", ("manifest_json",)
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"dictionary at {dict_path} has no manifest row — build was incomplete"
            )
        return json.loads(row[0])
    finally:
        conn.close()


def _compress_dict(src: Path, dst: Path) -> None:
    print(f"compressing {src} -> {dst} ...", file=sys.stderr)
    with src.open("rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)


def _emit_manifest(
    *,
    staging_dir: Path,
    gz_filename: str,
    gz_path: Path,
    local_manifest: dict,
) -> Path:
    """Write the sidecar MANIFEST.json the user-facing bootstrap consumes."""
    manifest = {
        "schema_version": local_manifest["schema_version"],
        "dictionary_version": local_manifest["dictionary_version"],
        "built_at": local_manifest["built_at"],
        "dictionary_filename": gz_filename,
        "dictionary_sha256": _sha256_of(gz_path),
        "dictionary_size_bytes": gz_path.stat().st_size,
        "compression": "gzip",
        "published_at": datetime.now(UTC).isoformat(),
    }
    out_path = staging_dir / "MANIFEST.json"
    out_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest written: {out_path}", file=sys.stderr)
    return out_path


# ---------------------------------------------------------------------------
# Direct HTTPS PUT upload (no local Globus endpoint required)
# ---------------------------------------------------------------------------


def _load_credentials() -> tuple[str, str]:
    """Resolve client_id + client_secret from env or OS keyring.

    Precedence: explicit env vars first; macOS Keyring / Linux Secret Service
    second. The keyring SERVICE name is checked in two places because there
    is a pre-existing inconsistency in the codebase: ``apecx-globus-setup``
    stores under ``nanobrain-globus`` (its actual implementation), but
    ``_globus_data_transfer._keyring_credentials_present`` checks
    ``apecx-globus-setup`` (the documented name). Until the two are
    reconciled upstream, this script tolerates both.
    """
    cid = os.environ.get("GLOBUS_COMPUTE_CLIENT_ID", "").strip()
    csecret = os.environ.get("GLOBUS_COMPUTE_CLIENT_SECRET", "").strip()
    if cid and csecret:
        return cid, csecret
    try:
        import keyring  # noqa: PLC0415

        for service in ("nanobrain-globus", "apecx-globus-setup"):
            cid = keyring.get_password(service, "client_id") or ""
            csecret = keyring.get_password(service, "client_secret") or ""
            if cid and csecret:
                return cid.strip(), csecret.strip()
    except ImportError:
        pass
    except Exception:
        pass
    raise RuntimeError(
        "--upload requires GLOBUS_COMPUTE_CLIENT_ID + GLOBUS_COMPUTE_CLIENT_SECRET "
        "either in env or stored via `apecx-globus-setup store`."
    )


def _acquire_tokens(*, collection_uuid: str) -> tuple[str, str]:
    """Run client-credentials flow → return (transfer_token, https_token).

    Reads creds from env OR apecx-globus-setup keyring. Both scopes are
    requested in ONE token call so we make a single round trip to Globus Auth.
    """
    try:
        import globus_sdk
    except ImportError as exc:
        raise RuntimeError(
            "--upload requires the globus_sdk package; install it via `pip install globus-sdk`"
        ) from exc

    client_id, client_secret = _load_credentials()

    auth_client = globus_sdk.ConfidentialAppAuthClient(client_id, client_secret)
    transfer_scope = "urn:globus:auth:scope:transfer.api.globus.org:all"
    https_scope = f"https://auth.globus.org/scopes/{collection_uuid}/https"
    try:
        response = auth_client.oauth2_client_credentials_tokens(
            requested_scopes=[transfer_scope, https_scope]
        )
    except globus_sdk.AuthAPIError as exc:
        raise RuntimeError(
            f"Globus Auth rejected client_credentials request: {exc}. "
            f"Confirm the confidential client has consent for both "
            f"the Transfer scope and the HTTPS scope of collection "
            f"{collection_uuid}."
        ) from exc

    by_rs = response.by_resource_server
    if "transfer.api.globus.org" not in by_rs:
        raise RuntimeError(
            "Globus Auth response missing Transfer-scope token; confidential "
            "client may lack consent."
        )
    if collection_uuid not in by_rs:
        raise RuntimeError(
            f"Globus Auth response missing HTTPS-scope token for collection "
            f"{collection_uuid}; confidential client may lack consent on the "
            f"data-access scope. Re-register or grant consent via "
            f"https://app.globus.org/settings/developers."
        )
    return (
        by_rs["transfer.api.globus.org"]["access_token"],
        by_rs[collection_uuid]["access_token"],
    )


def _discover_https_server(*, collection_uuid: str, transfer_token: str) -> str:
    """Query Transfer API for the collection's https_server URL."""
    import globus_sdk

    tc = globus_sdk.TransferClient(authorizer=globus_sdk.AccessTokenAuthorizer(transfer_token))
    endpoint = tc.get_endpoint(collection_uuid)
    https_server = endpoint["https_server"]
    if not https_server:
        raise RuntimeError(
            f"collection {collection_uuid} has no https_server configured; "
            f"the collection admin must enable HTTPS on the Globus Connect "
            f"Server before this script can publish."
        )
    return str(https_server)


def _http_put(*, url: str, local_path: Path, https_token: str, quiet: bool) -> None:
    """Stream ``local_path`` to ``url`` via PUT with bearer auth.

    Uses urllib so we don't add an httpx/requests dep here. The full file
    is sent in one PUT — Globus HTTPS doesn't require chunked uploads for
    files of this size (~45 MB), and most Globus Connect Server deployments
    cap at multi-GB single requests.
    """
    if not quiet:
        sys.stderr.write(f"PUT  {url}\n")
        sys.stderr.write(f"     ({local_path.stat().st_size:,} bytes)\n")
    with local_path.open("rb") as fh:
        data = fh.read()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {https_token}",
            "Content-Type": "application/octet-stream",
            "User-Agent": "apecx-publish-dictionary/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            status = resp.status
            body = resp.read(1024)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:1024]
        raise RuntimeError(f"HTTPS PUT {url} -> HTTP {exc.code}: {body!r}") from exc
    if status not in (200, 201, 204):
        raise RuntimeError(f"HTTPS PUT {url} -> HTTP {status}: {body!r}")
    if not quiet:
        sys.stderr.write(f"     -> HTTP {status} OK\n")


def upload_via_https(
    *,
    collection_uuid: str,
    files: list[tuple[Path, str]],
    quiet: bool = False,
) -> str:
    """Upload ``files`` to the collection via direct HTTPS PUT.

    Returns the discovered https_server URL so the caller can record it
    or echo it for verification.

    Each entry of ``files`` is ``(local_path, remote_path_under_collection)``.
    The remote path MUST begin with a leading slash; the script does not
    join it relative to any default directory.
    """
    transfer_token, https_token = _acquire_tokens(collection_uuid=collection_uuid)
    https_server = _discover_https_server(
        collection_uuid=collection_uuid,
        transfer_token=transfer_token,
    )
    if not quiet:
        sys.stderr.write(f"https_server: {https_server}\n")
    base = https_server.rstrip("/")
    for local_path, remote_path in files:
        if not remote_path.startswith("/"):
            raise ValueError(f"remote path must start with '/'; got {remote_path!r}")
        _http_put(
            url=f"{base}{remote_path}",
            local_path=local_path,
            https_token=https_token,
            quiet=quiet,
        )
    return https_server


def _print_manual_instructions(*, gz_path: Path, manifest_path: Path, dest_path: str) -> None:
    """Print equivalent globus-cli commands for operators without
    client_credentials configured."""
    print(
        "\n--- Manual publish via globus-cli (requires `globus login` done) ---",
        file=sys.stderr,
    )
    print(
        "# Both files are PUT directly to the collection over HTTPS — no\n"
        "# local Globus endpoint needed. Use 'globus transfer' as a fallback\n"
        "# when HTTPS auth is unavailable.\n",
        file=sys.stderr,
    )
    print(
        f"globus https --endpoint {APECX_COLLECTION_UUID} "
        f"put {gz_path} {dest_path.rstrip('/')}/{gz_path.name}",
        file=sys.stderr,
    )
    print(
        f"globus https --endpoint {APECX_COLLECTION_UUID} "
        f"put {manifest_path} {dest_path.rstrip('/')}/MANIFEST.json",
        file=sys.stderr,
    )
    print("--- End manual instructions ---\n", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dict-path",
        type=Path,
        default=Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite",
        help="Local dictionary SQLite (default: ~/.apecx/dictionary/dictionary.sqlite)",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        required=True,
        help="Temp directory where the gz + MANIFEST.json are staged. Will be created if absent.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Direct HTTPS PUT to the collection after compress + manifest "
        "(requires GLOBUS_COMPUTE_CLIENT_ID/SECRET)",
    )
    parser.add_argument(
        "--collection-uuid",
        default=APECX_COLLECTION_UUID,
        help=f"Globus collection UUID (default: {APECX_COLLECTION_UUID}, "
        f"the APECx Data at Argonne LCF collection)",
    )
    parser.add_argument(
        "--dest-path",
        default="/apecx-ramanathan-anl/public/synonyms_dictionary",
        help="Path inside the collection (default: "
        "/apecx-ramanathan-anl/public/synonyms_dictionary)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output during upload",
    )
    args = parser.parse_args(argv)

    if not args.dict_path.exists():
        print(f"ERROR: dict not found at {args.dict_path}", file=sys.stderr)
        return 2

    print(f"reading local manifest from {args.dict_path}", file=sys.stderr)
    local_manifest = _read_local_manifest(args.dict_path)
    version = local_manifest["dictionary_version"]
    print(f"local dictionary_version = {version}", file=sys.stderr)

    args.staging_dir.mkdir(parents=True, exist_ok=True)
    gz_filename = f"dictionary-{version}.sqlite.gz"
    gz_path = args.staging_dir / gz_filename
    _compress_dict(args.dict_path, gz_path)
    print(
        f"compressed: {args.dict_path.stat().st_size:,} bytes -> "
        f"{gz_path.stat().st_size:,} bytes "
        f"({100 * gz_path.stat().st_size / args.dict_path.stat().st_size:.1f}%)",
        file=sys.stderr,
    )

    manifest_path = _emit_manifest(
        staging_dir=args.staging_dir,
        gz_filename=gz_filename,
        gz_path=gz_path,
        local_manifest=local_manifest,
    )

    if args.upload:
        files = [
            (gz_path, f"{args.dest_path.rstrip('/')}/{gz_filename}"),
            (manifest_path, f"{args.dest_path.rstrip('/')}/MANIFEST.json"),
        ]
        try:
            https_server = upload_via_https(
                collection_uuid=args.collection_uuid,
                files=files,
                quiet=args.quiet,
            )
        except RuntimeError as exc:
            print(f"\nupload failed: {exc}", file=sys.stderr)
            print(
                f"files are staged at {args.staging_dir} — retry, or use the manual command below.",
                file=sys.stderr,
            )
            _print_manual_instructions(
                gz_path=gz_path,
                manifest_path=manifest_path,
                dest_path=args.dest_path,
            )
            return 1
        print(
            f"\nupload complete via {https_server}\n"
            f"  user-facing arm should set APECX_DICT_PUBLIC_BASE_URL=\n"
            f"  {https_server.rstrip('/')}{args.dest_path.rstrip('/')}",
            file=sys.stderr,
        )
    else:
        print(
            "\nStaged files (run with --upload to push):",
            file=sys.stderr,
        )
        print(f"  {gz_path}", file=sys.stderr)
        print(f"  {manifest_path}", file=sys.stderr)
        _print_manual_instructions(
            gz_path=gz_path,
            manifest_path=manifest_path,
            dest_path=args.dest_path,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
