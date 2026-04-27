"""
apecx-setup — interactive VIOLIN + BV-BRC data initializer.

Downloads apecx-data.tar.gz from the private apecx-data GitHub release
and extracts it to a user-chosen directory.  The directory path should
be exported as APECX_DATA_ROOT in the Claude Desktop MCP env block so
the database tools can find the files.

Requires ``gh`` (GitHub CLI) authenticated against AlexandrNP's org.
Install: https://cli.github.com   Authenticate: gh auth login
"""

import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

_DATA_REPO = "AlexandrNP/apecx-data"
_RELEASE_TAG = "v1.0.0"
_ASSET_NAME = "apecx-data.tar.gz"
_DEFAULT_DATA_DIR = Path.home() / ".apecx" / "data"

# Files extracted from the tarball; used only in the completion summary.
_EXPECTED_FILES = [
    "violin/Vaccine_Information.csv",
    "violin/Pathogen_Information.csv",
    "violin/Gene_Information.csv",
    "violin/Vaccine_Pathogen_Information.csv",
    "violin/Gene_Vaccine_Pathogen_Information.csv",
    "BVBRC_genome_alphavirus.csv",
]


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh_authenticated() -> bool:
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return result.returncode == 0


def _download_asset(dest_dir: str) -> None:
    subprocess.run(
        [
            "gh",
            "release",
            "download",
            _RELEASE_TAG,
            "--repo",
            _DATA_REPO,
            "--pattern",
            _ASSET_NAME,
            "--dir",
            dest_dir,
            "--clobber",
        ],
        check=True,
    )


def main() -> None:
    print("apecx-setup — VIOLIN + BV-BRC data initializer")
    print()

    if not _gh_available():
        print("ERROR: 'gh' (GitHub CLI) is required to download the data.")
        print("       Install it at https://cli.github.com, then run: gh auth login")
        sys.exit(1)

    if not _gh_authenticated():
        print("ERROR: 'gh' is not authenticated with a GitHub account.")
        print("       Run: gh auth login")
        sys.exit(1)

    raw = input(f"Data directory [{_DEFAULT_DATA_DIR}]: ").strip()
    data_dir = Path(raw).expanduser() if raw else _DEFAULT_DATA_DIR

    if data_dir.exists():
        existing_csvs = list(data_dir.glob("**/*.csv"))
        if existing_csvs:
            answer = (
                input(
                    f"  {data_dir} already contains {len(existing_csvs)} CSV file(s). Overwrite? [y/N] "
                )
                .strip()
                .lower()
            )
            if answer != "y":
                print("Aborted.")
                sys.exit(0)

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"Downloading {_ASSET_NAME} from {_DATA_REPO} @ {_RELEASE_TAG} ...")
        try:
            _download_asset(tmpdir)
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: download failed (exit {exc.returncode}).")
            print("       Check that your gh account has access to AlexandrNP/apecx-data.")
            sys.exit(1)

        archive = Path(tmpdir) / _ASSET_NAME
        if not archive.exists():
            print(f"ERROR: {_ASSET_NAME} not found after download in {tmpdir}")
            sys.exit(1)

        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"Extracting to {data_dir} ...")
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(path=data_dir, filter="data")

    missing = [f for f in _EXPECTED_FILES if not (data_dir / f).exists()]
    if missing:
        print(f"WARNING: {len(missing)} expected file(s) not found after extraction:")
        for f in missing:
            print(f"  {data_dir / f}")
    else:
        print(f"All {len(_EXPECTED_FILES)} data files extracted successfully.")

    print()
    print("Add APECX_DATA_ROOT to your Claude Desktop MCP config:")
    print()
    print('  "env": {')
    print(f'      "APECX_DATA_ROOT": "{data_dir}",')
    print("      ...")
    print("  }")
    print()
    print("Then fully quit and relaunch Claude Desktop.")
