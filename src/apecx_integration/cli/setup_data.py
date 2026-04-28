"""
apecx-setup — interactive VIOLIN + BV-BRC data initializer.

Downloads apecx-data.tar.gz from the private apecx-data GitHub release,
extracts it to a user-chosen directory, and (optionally) patches the
Claude Desktop MCP config so APECX_DATA_ROOT points at the new path.

Requires ``gh`` (GitHub CLI) authenticated against AlexandrNP's org.
Install: https://cli.github.com   Authenticate: gh auth login
"""

import json
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

# Defaults used only when creating a brand-new ``apecx`` MCP block from
# scratch.  When the block already exists, we touch only APECX_DATA_ROOT.
_DEFAULT_LLM_ENV = {
    "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
    "APECX_LLM_MODEL": "mistral-nemo:latest",
    "APECX_LLM_API_KEY": "unused",
}


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


def _default_claude_config_path() -> Path:
    """The standard Claude Desktop config location for this OS."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        import os

        return (
            Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude/claude_desktop_config.json"
        )
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _find_apecx_mcp_binary() -> str | None:
    """Locate the ``apecx-mcp`` binary so a new config block can point at it."""
    found = shutil.which("apecx-mcp")
    if found:
        return found
    candidate = Path.home() / ".local/bin/apecx-mcp"
    if candidate.exists():
        return str(candidate)
    return None


def _update_claude_config(config_path: Path, data_dir: Path) -> str:
    """Patch the Claude Desktop config to set APECX_DATA_ROOT.

    Behavior:
      - Missing file: create a minimal config with a full ``apecx`` block.
      - Missing ``mcpServers``: add it.
      - Missing ``mcpServers.apecx``: add a full block (defaults + the data dir).
      - Existing ``mcpServers.apecx``: update only ``env.APECX_DATA_ROOT``;
        preserve every other field (command, args, other env vars).

    Returns a one-line summary of what changed.
    """
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Existing config at {config_path} is not valid JSON: {exc}. "
                "Refusing to overwrite — please fix or delete it first."
            ) from exc
        if not isinstance(config, dict):
            raise RuntimeError(f"Existing config at {config_path} is not a JSON object.")
    else:
        config = {}

    mcp_servers = config.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        raise RuntimeError(f"Existing config at {config_path} has a non-object 'mcpServers' value.")

    apecx_block = mcp_servers.get("apecx")
    if apecx_block is None:
        apecx_mcp = _find_apecx_mcp_binary()
        if apecx_mcp is None:
            raise RuntimeError(
                "Could not locate the apecx-mcp binary. Install it first "
                "(uv tool install / pipx install) before running apecx-setup."
            )
        mcp_servers["apecx"] = {
            "command": apecx_mcp,
            "args": [],
            "env": {"APECX_DATA_ROOT": str(data_dir), **_DEFAULT_LLM_ENV},
        }
        change = f"created new 'apecx' server entry pointing at {apecx_mcp}"
    else:
        if not isinstance(apecx_block, dict):
            raise RuntimeError(f"Existing 'mcpServers.apecx' in {config_path} is not an object.")
        env = apecx_block.setdefault("env", {})
        if not isinstance(env, dict):
            raise RuntimeError(
                f"Existing 'mcpServers.apecx.env' in {config_path} is not an object."
            )
        prior = env.get("APECX_DATA_ROOT")
        env["APECX_DATA_ROOT"] = str(data_dir)
        if prior == str(data_dir):
            change = f"APECX_DATA_ROOT already set to {data_dir} (no change)"
        elif prior is None:
            change = f"added APECX_DATA_ROOT={data_dir} to existing 'apecx' server"
        else:
            change = f"updated APECX_DATA_ROOT: {prior} -> {data_dir}"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return change


def _prompt_yes_no(question: str, default: bool) -> bool:
    """Default-aware y/n prompt; empty input picks the default."""
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _maybe_update_claude_config(data_dir: Path) -> None:
    """Find the Claude Desktop config and offer to patch APECX_DATA_ROOT."""
    print()
    print("Claude Desktop config update")
    default_path = _default_claude_config_path()

    if default_path.exists():
        print(f"  Found config: {default_path}")
        if not _prompt_yes_no("  Update this config to set APECX_DATA_ROOT?", default=True):
            print("  Skipped. Set APECX_DATA_ROOT manually in your Claude Desktop config.")
            return
        target = default_path
    else:
        print(f"  No config at the default location ({default_path}).")
        if not _prompt_yes_no("  Specify a config path manually?", default=False):
            print("  Skipped. Add the apecx MCP server to your Claude Desktop config manually.")
            return
        raw = input("  Config path: ").strip()
        if not raw:
            print("  Empty path — skipping config update.")
            return
        target = Path(raw).expanduser()

    try:
        change = _update_claude_config(target, data_dir)
    except RuntimeError as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)
    except OSError as exc:
        print(f"  ERROR: could not write {target}: {exc}")
        sys.exit(1)

    print(f"  OK — {change}")
    print(f"  Wrote {target}")
    print("  Fully quit and relaunch Claude Desktop for the change to take effect.")


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

    _maybe_update_claude_config(data_dir)
