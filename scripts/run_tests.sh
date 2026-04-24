#!/usr/bin/env bash
# Canonical invocation of the full test suite.
#
# Runs pytest under the project venv with PYTHONPATH wired so
# editable installs resolve correctly. Avoids the "system conda
# python" footgun (friction log #14) and the "rootdir drift"
# one (friction log #3).
#
# Usage:
#     scripts/run_tests.sh              # full suite
#     scripts/run_tests.sh tests/unit   # a subset
#
# Env vars honored by individual tests:
#
#   APECX_LLM_BASE_URL, APECX_LLM_MODEL, APECX_LLM_TEMPERATURE,
#   APECX_LLM_MAX_TOKENS, APECX_LLM_API_KEY
#       → unlock live-LLM integration tests (otherwise they
#       auto-skip when ollama is unreachable).
#
#   APECX_RUN_AC8_WALLTIME=1
#       → opt into the operator-run AC8 wall-time benchmark.
#       Skipped by default since it's model+hardware sensitive.
#
#   APECX_T12_RUN_LIVE_LLM=1
#       → opt into live-LLM T12 reproducibility fixtures (not
#       required for the 3 shipped placeholder fixtures).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "error: $VENV_PY not found. Create the venv with:" >&2
    echo "   python -m venv .venv && .venv/bin/pip install -e .[dev]" >&2
    exit 1
fi

cd "$REPO_ROOT"
exec env PYTHONPATH=src "$VENV_PY" -m pytest "$@"
