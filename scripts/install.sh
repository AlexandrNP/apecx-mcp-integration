#!/usr/bin/env bash
# install.sh — one-shot installer for apecx-mcp.
#
# What it does:
#   1. Picks the right installer (uv tool > pipx > python -m pip --user).
#   2. Pulls apecx-mcp-integration + the two sibling repos
#      (nanobrain @ academy-integration, apecx-harvesters @ main) from
#      git in a single command — no manual clones.
#   3. Prints the Claude Desktop config snippet ready to paste.
#
# Prerequisites (NOT installed by this script):
#   - Python >= 3.12
#   - Ollama running on localhost:11434 with mistral-nemo:latest pulled
#     (or another OpenAI-compatible LLM endpoint you configure via
#     APECX_LLM_BASE_URL / APECX_LLM_MODEL)
#   - Docker is OPTIONAL — the autostart backend uses SQLite by
#     default. Only set APECX_CP_POSTGRES_URL if you want Postgres.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/AlexandrNP/apecx-mcp-integration/main/scripts/install.sh | bash
#   # or, after cloning:
#   bash scripts/install.sh
#
# Customize the branch via env var:
#   APECX_BRANCH=main bash scripts/install.sh

set -euo pipefail

REPO_URL="${APECX_REPO_URL:-https://github.com/AlexandrNP/apecx-mcp-integration.git}"
BRANCH="${APECX_BRANCH:-main}"

GIT_SPEC="git+${REPO_URL}@${BRANCH}"

echo "==> apecx-mcp installer"
echo "    source: ${GIT_SPEC}"
echo

# 1. Pick the installer.
if command -v uv >/dev/null 2>&1; then
    INSTALL_CMD=(uv tool install --python 3.12 --reinstall "${GIT_SPEC}")
    INSTALLER="uv tool"
elif command -v pipx >/dev/null 2>&1; then
    INSTALL_CMD=(pipx install --force "${GIT_SPEC}")
    INSTALLER="pipx"
else
    echo "WARNING: neither uv nor pipx found." >&2
    echo "Falling back to ``python3 -m pip install --user ...`` (does not isolate the install)." >&2
    echo "Recommended: install uv first --" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    INSTALL_CMD=(python3.12 -m pip install --user --upgrade "${GIT_SPEC}")
    INSTALLER="pip --user"
fi

echo "==> Installing via ${INSTALLER}"
"${INSTALL_CMD[@]}"
echo

# 2. Locate the installed binary.
APECX_MCP_BIN="$(command -v apecx-mcp 2>/dev/null || true)"
if [ -z "${APECX_MCP_BIN}" ]; then
    # uv tool installs to ~/.local/bin which may not be on PATH yet.
    if [ -x "${HOME}/.local/bin/apecx-mcp" ]; then
        APECX_MCP_BIN="${HOME}/.local/bin/apecx-mcp"
        echo "NOTE: ${APECX_MCP_BIN} is not on your PATH yet." >&2
        echo "      Add ~/.local/bin to PATH OR use the absolute path below in the Claude Desktop config." >&2
        echo
    fi
fi

if [ -z "${APECX_MCP_BIN}" ]; then
    echo "ERROR: apecx-mcp not found after install. The install command above may have failed silently." >&2
    exit 1
fi

# 3. Offer data-directory setup.
APECX_SETUP_BIN="$(command -v apecx-setup 2>/dev/null || true)"
if [ -z "${APECX_SETUP_BIN}" ] && [ -x "${HOME}/.local/bin/apecx-setup" ]; then
    APECX_SETUP_BIN="${HOME}/.local/bin/apecx-setup"
fi

echo "==> Data setup (VIOLIN + BV-BRC)"
echo "    The database query tools need local CSV files to work."
echo "    Requires: gh (GitHub CLI) authenticated — https://cli.github.com"
echo
printf "    Download data now? [y/N] "
read -r _DATA_ANSWER
if [ "${_DATA_ANSWER}" = "y" ] || [ "${_DATA_ANSWER}" = "Y" ]; then
    if [ -n "${APECX_SETUP_BIN}" ]; then
        "${APECX_SETUP_BIN}"
        APECX_DATA_DIR="${HOME}/.apecx/data"   # default; user may have changed it
    else
        echo "WARNING: apecx-setup binary not found. Run it manually after install:" >&2
        echo "  apecx-setup" >&2
        APECX_DATA_DIR=""
    fi
else
    echo "    Skipped. Run 'apecx-setup' at any time to download the data."
    APECX_DATA_DIR=""
fi
echo

# 4. Print the Claude Desktop config block.
# Build the env block — include APECX_DATA_ROOT only when data was downloaded.
if [ -n "${APECX_DATA_DIR}" ]; then
    _DATA_ENV_LINE="        \"APECX_DATA_ROOT\": \"${APECX_DATA_DIR}\","
else
    _DATA_ENV_LINE="        // \"APECX_DATA_ROOT\": \"/path/to/data\",  <- run apecx-setup then set this"
fi

cat <<EOF
==> Install complete.

Installed binary: ${APECX_MCP_BIN}

Add this block to your Claude Desktop config:
  macOS:    ~/Library/Application Support/Claude/claude_desktop_config.json
  Windows:  %APPDATA%\\Claude\\claude_desktop_config.json
  Linux:    ~/.config/Claude/claude_desktop_config.json

If the file already has an "mcpServers" key, add the "apecx" entry as
a child of that key (NOT as a sibling — Claude Desktop only scans
inside mcpServers).

------- copy from here -------
{
  "mcpServers": {
    "apecx": {
      "command": "${APECX_MCP_BIN}",
      "args": [],
      "env": {
${_DATA_ENV_LINE}
        "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
        "APECX_LLM_MODEL": "mistral-nemo:latest",
        "APECX_LLM_API_KEY": "unused"
      }
    }
  }
}
------- copy to here -------

Then fully quit and relaunch Claude Desktop.

The 20 apecx tools (start_workflow, show_diff, execute_workflow,
list_workflows, describe_workflow, query_vaccines, query_pathogens,
query_genes, query_bvbrc_genomes, get_vaccine_pathogen_genes,
resolve_entity, database_statistics, list_pending_approvals,
approve, reject, correct, estimate_cost, confirm_allocation,
export_hpc_bundle, ingest_hpc_bundle) will appear in Claude
Desktop's tool picker.

The database tools (query_*) require APECX_DATA_ROOT to point at
the data directory populated by 'apecx-setup'.

The Control Plane backend autostarts on the first MCP server
launch; SQLite at \$PWD/apecx_cp.db. No Docker needed.

If tools don't appear after relaunch, tail the Claude Desktop log:
  macOS:    ~/Library/Logs/Claude/mcp-server-apecx.log
  Windows:  %LOCALAPPDATA%\\Claude\\Logs\\mcp-server-apecx.log
EOF
