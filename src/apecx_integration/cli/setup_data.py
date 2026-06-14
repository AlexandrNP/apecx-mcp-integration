"""
apecx-setup data — shared data-acquisition helpers.

The actual data acquisition is now Globus-only (the legacy ``gh release
download`` path was retired 2026-05-21 — see ``cli/setup.py:_step_data`` and
``cli/_globus_data_transfer.py``). This module no longer downloads anything; it
provides the operator-facing pieces the Globus path reuses:

  * ``prompt_for_data_dir`` — choose the local data directory (with an
    overwrite guard).
  * ``_maybe_update_claude_config`` — patch the Claude Desktop MCP config so
    ``APECX_DATA_ROOT`` points at the chosen directory.
  * ``_EXPECTED_FILES`` / ``_DEFAULT_DATA_DIR`` — the post-transfer layout the
    rest of the stack expects.
  * ``_run_reconfigure_llm`` — the ``apecx-setup --reconfigure-llm`` flow.

The canonical entry point is ``apecx-setup`` (``cli/setup.py:main``); this
module is imported by it, not run directly.
"""

import json
import shutil
import sys
from pathlib import Path

_DEFAULT_DATA_DIR = Path.home() / ".apecx" / "data"

# The post-transfer layout the rest of the stack expects (``apecx_db_integration``
# etc.). Used by the Globus transfer's dest mapping + the completion summary.
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


def prompt_for_data_dir(*, interactive: bool = True) -> Path | None:
    """Choose the local data directory; guard against clobbering existing CSVs.

    Returns the chosen ``Path``, or ``None`` if the operator aborted at the
    overwrite prompt. In non-interactive mode returns ``_DEFAULT_DATA_DIR``
    without prompting (callers handle the no-prompt policy upstream).

    Relocated here (2026-05-21) from the retired gh ``_run_full_setup`` so the
    Globus data path can reuse the SAME prompt + overwrite guard — previously
    the Globus path hardcoded ``~/.apecx/data`` and skipped both.
    """
    if not interactive:
        return _DEFAULT_DATA_DIR

    raw = input(f"Data directory [{_DEFAULT_DATA_DIR}]: ").strip()
    data_dir = Path(raw).expanduser() if raw else _DEFAULT_DATA_DIR

    if data_dir.exists():
        existing_csvs = list(data_dir.glob("**/*.csv"))
        if existing_csvs:
            answer = (
                input(
                    f"  {data_dir} already contains {len(existing_csvs)} CSV "
                    "file(s). Overwrite? [y/N] "
                )
                .strip()
                .lower()
            )
            if answer != "y":
                print("Aborted.")
                return None
    return data_dir


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


def _build_apecx_server_block(data_dir: Path, llm_env: dict | None = None) -> dict:
    """Construct the full ``mcpServers.apecx`` block for a fresh install.

    ``llm_env`` overrides the LLM defaults; pass the result of
    ``_prompt_for_llm_config()`` for the interactive first-install path.
    When None, falls back to ``_DEFAULT_LLM_ENV`` (used by tests and
    non-interactive callers).

    Raises RuntimeError if apecx-mcp can't be located on disk.
    """
    apecx_mcp = _find_apecx_mcp_binary()
    if apecx_mcp is None:
        raise RuntimeError(
            "Could not locate the apecx-mcp binary. Install it first "
            "(uv tool install / pipx install) before running apecx-setup."
        )
    env = dict(_DEFAULT_LLM_ENV)
    if llm_env is not None:
        env.update(llm_env)
    env["APECX_DATA_ROOT"] = str(data_dir)
    return {
        "command": apecx_mcp,
        "args": [],
        "env": env,
    }


def _prompt_for_llm_config() -> dict:
    """Prompt for the three LLM env vars; Enter accepts each default.

    Empty input picks the default verbatim — the common Ollama path is
    three Enter presses.  Non-empty input is taken literally with no
    URL/model validation; we trust the operator and surface mistakes
    later (apecx-mcp will fail loudly at first composer call).
    """
    print()
    print("LLM configuration")
    print("  apecx-mcp uses an OpenAI-compatible LLM endpoint for the composer.")
    print("  Defaults assume Ollama on localhost:11434.  Press Enter to accept,")
    print("  or type a replacement.  Re-running apecx-setup later will not")
    print("  re-prompt — edit the values directly in claude_desktop_config.json.")
    print()

    chosen: dict[str, str] = {}
    for key in ("APECX_LLM_BASE_URL", "APECX_LLM_MODEL", "APECX_LLM_API_KEY"):
        default = _DEFAULT_LLM_ENV[key]
        raw = input(f"  {key} [{default}]: ").strip()
        chosen[key] = raw or default
    return chosen


def _load_or_init_config(config_path: Path) -> dict:
    """Load the Claude Desktop config, or return ``{}`` if missing.

    Raises RuntimeError on malformed JSON or non-object root.
    """
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Existing config at {config_path} is not valid JSON: {exc}. "
            "Refusing to overwrite — please fix or delete it first."
        ) from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"Existing config at {config_path} is not a JSON object.")
    return config


def _apecx_block_state(config: dict, config_path: Path) -> dict | None:
    """Return the existing ``mcpServers.apecx`` block, or None if absent.

    Validates that ``mcpServers`` and the apecx block (when present) are
    JSON objects.  Raises RuntimeError on shape mismatch.
    """
    mcp_servers = config.get("mcpServers")
    if mcp_servers is None:
        return None
    if not isinstance(mcp_servers, dict):
        raise RuntimeError(f"Existing config at {config_path} has a non-object 'mcpServers' value.")
    apecx_block = mcp_servers.get("apecx")
    if apecx_block is None:
        return None
    if not isinstance(apecx_block, dict):
        raise RuntimeError(f"Existing 'mcpServers.apecx' in {config_path} is not an object.")
    return apecx_block


def _update_claude_config(
    config_path: Path,
    data_dir: Path,
    llm_env: dict | None = None,
) -> str:
    """Patch the Claude Desktop config to set APECX_DATA_ROOT.

    Behavior:
      - Missing file: create a minimal config with a full ``apecx`` block.
      - Missing ``mcpServers``: add it.
      - Missing ``mcpServers.apecx``: add a full block (defaults + the data dir).
        ``llm_env`` (when provided) overrides APECX_LLM_BASE_URL/_MODEL/_API_KEY
        in the new block — used by the interactive first-install path.
      - Existing ``mcpServers.apecx``: update only ``env.APECX_DATA_ROOT``;
        preserve every other field (command, args, other env vars).
        ``llm_env`` is ignored on the update path — we never overwrite an
        existing operator's LLM settings.

    Returns a one-line summary of what changed.
    """
    config = _load_or_init_config(config_path)
    apecx_block = _apecx_block_state(config, config_path)

    mcp_servers = config.setdefault("mcpServers", {})
    if apecx_block is None:
        mcp_servers["apecx"] = _build_apecx_server_block(data_dir, llm_env=llm_env)
        change = f"created new 'apecx' server entry pointing at {mcp_servers['apecx']['command']}"
    else:
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


def _resolve_config_target(default_path: Path) -> Path | None:
    """Confirm the standard config path or prompt for a manual path.

    Returns the resolved Path, or None if the user declined to specify one.
    """
    if default_path.exists():
        print(f"  Found config: {default_path}")
        if _prompt_yes_no("  Use this config?", default=True):
            return default_path
        raw = input("  Alternate config path (blank to skip): ").strip()
        return Path(raw).expanduser() if raw else None

    print(f"  No config at the default location ({default_path}).")
    if not _prompt_yes_no("  Specify a config path manually?", default=False):
        return None
    raw = input("  Config path: ").strip()
    return Path(raw).expanduser() if raw else None


def _format_apecx_block_preview(block: dict) -> str:
    """Pretty-print the proposed apecx block for the first-time prompt."""
    return json.dumps({"mcpServers": {"apecx": block}}, indent=2)


def _maybe_update_claude_config(data_dir: Path) -> None:
    """Find the Claude Desktop config and offer to patch / install the apecx block.

    Two distinct flows depending on whether ``mcpServers.apecx`` already
    exists in the target config:

    First-time install (apecx block absent):
      - Show the FULL proposed JSON block (command, args, env vars).
      - Warn that LLM defaults assume Ollama on localhost:11434.
      - Confirm before writing.

    Update (apecx block present):
      - Touch only ``env.APECX_DATA_ROOT``; preserve everything else.
      - Confirm before writing.
    """
    print()
    print("Claude Desktop config update")

    target = _resolve_config_target(_default_claude_config_path())
    if target is None:
        print("  Skipped. Update your Claude Desktop config manually.")
        return

    # Inspect existing state to choose the right flow + prompt.
    try:
        existing = _load_or_init_config(target)
        apecx_existing = _apecx_block_state(existing, target)
    except RuntimeError as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)

    llm_env: dict | None = None
    if apecx_existing is None:
        # First-time install: prompt for LLM config FIRST so the preview
        # reflects the operator's actual choices, not the defaults.
        print()
        print("  No 'apecx' MCP server found — this is a first-time install.")
        llm_env = _prompt_for_llm_config()

        try:
            proposed_block = _build_apecx_server_block(data_dir, llm_env=llm_env)
        except RuntimeError as exc:
            print(f"  ERROR: {exc}")
            sys.exit(1)

        print()
        print("  The following block will be ADDED to mcpServers:")
        print()
        for line in _format_apecx_block_preview(proposed_block).splitlines():
            print(f"    {line}")
        print()
        if not _prompt_yes_no("  Add this block to your Claude Desktop config?", default=True):
            print("  Skipped. Add the apecx MCP server to your config manually.")
            return
    else:
        # Update flow: touch only APECX_DATA_ROOT.
        prior = (
            apecx_existing.get("env", {}).get("APECX_DATA_ROOT")
            if isinstance(apecx_existing.get("env"), dict)
            else None
        )
        print()
        print("  Existing 'apecx' MCP server found.")
        if prior == str(data_dir):
            print(f"  APECX_DATA_ROOT is already set to {data_dir}. Nothing to do.")
            print("  (To change LLM settings, run: apecx-setup --reconfigure-llm)")
            return
        if prior is None:
            print(f"  Will ADD APECX_DATA_ROOT = {data_dir}")
        else:
            print(f"  Will CHANGE APECX_DATA_ROOT: {prior} -> {data_dir}")
        print("  All other fields (command, args, LLM env vars) will be preserved.")
        print("  (To change LLM settings, run: apecx-setup --reconfigure-llm)")
        print()
        if not _prompt_yes_no("  Apply this change?", default=True):
            print("  Skipped. Set APECX_DATA_ROOT manually in your Claude Desktop config.")
            return

    try:
        change = _update_claude_config(target, data_dir, llm_env=llm_env)
    except RuntimeError as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)
    except OSError as exc:
        print(f"  ERROR: could not write {target}: {exc}")
        sys.exit(1)

    print(f"  OK — {change}")
    print(f"  Wrote {target}")
    print("  Fully quit and relaunch Claude Desktop for the change to take effect.")


def _reconfigure_llm_in_config(config_path: Path) -> None:
    """Re-prompt for LLM env vars in an existing apecx config block.

    Standalone path — does NOT download data, does NOT touch
    APECX_DATA_ROOT, command, args, or unrelated env vars.  Errors out
    if no apecx block exists; the user must run plain ``apecx-setup``
    first.

    Prompts prefill with the *current* config values (not Ollama
    defaults), so the operator can see what they have and only change
    what they want.  API keys are shown in the prompt — they live in
    plaintext in the file already, and masking would prevent the user
    from confirming the value they're keeping.
    """
    print("apecx-setup --reconfigure-llm — update LLM env in Claude Desktop config")
    print()

    try:
        config = _load_or_init_config(config_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    if not config_path.exists():
        print(f"ERROR: config file does not exist: {config_path}")
        print("       Run 'apecx-setup' (without --reconfigure-llm) first.")
        sys.exit(1)

    try:
        apecx_block = _apecx_block_state(config, config_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    if apecx_block is None:
        print(f"ERROR: no 'apecx' MCP server found in {config_path}.")
        print("       Run 'apecx-setup' (without --reconfigure-llm) first to install one.")
        sys.exit(1)

    current_env = apecx_block.get("env")
    if not isinstance(current_env, dict):
        print(f"ERROR: 'mcpServers.apecx.env' in {config_path} is missing or not an object.")
        sys.exit(1)

    print(f"  Editing config: {config_path}")
    print("  Current values shown in [brackets]. Press Enter to keep, type to replace.")
    print()

    new_env: dict[str, str] = {}
    for key in ("APECX_LLM_BASE_URL", "APECX_LLM_MODEL", "APECX_LLM_API_KEY"):
        current = current_env.get(key, _DEFAULT_LLM_ENV[key])
        raw = input(f"  {key} [{current}]: ").strip()
        new_env[key] = raw or current

    print()
    print("  Proposed changes:")
    any_change = False
    for key in ("APECX_LLM_BASE_URL", "APECX_LLM_MODEL", "APECX_LLM_API_KEY"):
        old = current_env.get(key, "<unset>")
        new = new_env[key]
        if old == new:
            print(f"    {key}: unchanged")
        else:
            any_change = True
            print(f"    {key}: {old} -> {new}")

    if not any_change:
        print()
        print("  No changes. Config not modified.")
        return

    print()
    if not _prompt_yes_no("  Apply these changes?", default=True):
        print("  Cancelled. Config not modified.")
        return

    current_env.update(new_env)
    try:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: could not write {config_path}: {exc}")
        sys.exit(1)
    print(f"  OK — wrote {config_path}")
    print("  Fully quit and relaunch Claude Desktop for the change to take effect.")


def _run_reconfigure_llm() -> None:
    """Entry point for ``apecx-setup --reconfigure-llm``.

    Resolves the Claude Desktop config path the same way the data-install
    flow does (default location → confirm; or prompt for alternate),
    then hands off to ``_reconfigure_llm_in_config``.
    """
    print("LLM reconfiguration — Claude Desktop config")
    target = _resolve_config_target(_default_claude_config_path())
    if target is None:
        print("  Skipped. No config selected.")
        return
    if not target.exists():
        print(f"ERROR: {target} does not exist.")
        print("       Run 'apecx-setup' (without --reconfigure-llm) first.")
        sys.exit(1)
    _reconfigure_llm_in_config(target)


def report_post_transfer_layout(data_dir: Path) -> list[str]:
    """Print which expected files are present under ``data_dir``; return the
    missing ones. Used by the Globus path's completion summary so the operator
    sees the same "all N files present" / "WARNING: missing" report the gh path
    used to print after extraction."""
    missing = [f for f in _EXPECTED_FILES if not (data_dir / f).exists()]
    if missing:
        print(f"WARNING: {len(missing)} expected file(s) not found under {data_dir}:")
        for f in missing:
            print(f"  {data_dir / f}")
    else:
        print(f"All {len(_EXPECTED_FILES)} data files present under {data_dir}.")
    return missing


def main(argv: list[str] | None = None) -> None:
    """Thin dispatcher kept for the ``--reconfigure-llm`` flow.

    Data acquisition moved to ``apecx-setup`` (``cli/setup.py``); the gh
    download path was retired 2026-05-21. ``argv`` is exposed for tests.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="apecx-setup-data",
        description=(
            "Reconfigure LLM env vars in an existing Claude Desktop config. "
            "For data acquisition, run `apecx-setup` (Globus-only since "
            "2026-05-21)."
        ),
    )
    parser.add_argument(
        "--reconfigure-llm",
        action="store_true",
        help=(
            "Re-prompt for APECX_LLM_BASE_URL / _MODEL / _API_KEY in an "
            "existing apecx server block. Errors if the config has no apecx "
            "block yet."
        ),
    )
    args = parser.parse_args(argv)

    if args.reconfigure_llm:
        _run_reconfigure_llm()
    else:
        print("Data acquisition is handled by `apecx-setup` (Globus-only).")
        print("Use `apecx-setup-data --reconfigure-llm` only to edit LLM env vars.")
