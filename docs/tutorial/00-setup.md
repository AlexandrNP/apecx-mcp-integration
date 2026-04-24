# 00 — Setup

Goal: from a clean laptop to a running APECx Control Plane + MCP
surface in under 20 minutes.

## What you need

- macOS or Linux with Python **3.12** (use `python --version` to check).
- Docker Desktop **or** Apptainer (HPC-flavored container runtime).
  The Control Plane's infra layer auto-detects either. Only required
  if you use the default managed-Postgres path — SQLite works without.
- Git.
- ~4 GB free disk.

You'll install Ollama too, but that's a later step.

## 1. Clone the repo

```bash
git clone https://github.com/AlexandrNP/apecx-mcp-integration.git
cd apecx-mcp-integration
```

If you'll also compose workflows against the real component library,
clone the sibling repos too:

```bash
cd ..
git clone https://github.com/AlexandrNP/nanobrain.git
git clone https://github.com/AlexandrNP/apecx-db-integration.git
cd apecx-mcp-integration
```

## 2. Create the project venv

```bash
python -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .[dev]
.venv/bin/pip install -e ../nanobrain
.venv/bin/pip install -e ../apecx-db-integration
```

The editable installs mean ``from apecx_integration import ...``,
``from nanobrain import ...``, and ``from apecx_db_integration import
...`` all resolve to the sibling checkouts. Changes in any of the
three repos take effect without re-installing.

## 3. Smoke-test the install

```bash
scripts/run_tests.sh tests/unit -q
```

Expected output: **all unit tests pass in a few seconds**. If you
see `ModuleNotFoundError`, you're probably running the system Python
— use `scripts/run_tests.sh` (it routes through `.venv/bin/python`
automatically). See `docs/session_friction_log.md` #14 for the
history.

## 4. Install Ollama + pull a model

The composer uses a local LLM to turn a scientist's natural-language
prompt into a workflow YAML. Default: Ollama + mistral-nemo.

```bash
# macOS
brew install ollama
# Linux
# See https://ollama.com/download

ollama serve &                             # background daemon
ollama pull mistral-nemo:latest           # ~7 GB
```

Verify:

```bash
curl -s http://localhost:11434/api/tags | jq '.models[].name'
# → "mistral-nemo:latest"
```

## 5. Start the Control Plane

In one terminal:

```bash
.venv/bin/apecx-cp serve
```

This auto-provisions a local Postgres container on first boot (or
uses SQLite if `APECX_CP_DB_URL=sqlite:///./cp.db` is set). Leave it
running. Verify:

```bash
curl -s http://localhost:8000/healthz
# → {"status": "ok", "phase": "scaffold"}
```

## 6. Start the MCP server

In another terminal:

```bash
APECX_CONTROL_PLANE_URL=http://localhost:8000 \
APECX_LLM_BASE_URL=http://localhost:11434/v1 \
APECX_LLM_MODEL=mistral-nemo:latest \
.venv/bin/apecx-mcp
```

The MCP server speaks stdio. Wire it into Claude Desktop via
Claude's MCP config, or drive it from the CLI next-step.

## Where you are now

- Control Plane serving on `:8000`
- Ollama serving on `:11434`
- MCP server ready to accept stdio

Next file: `01-first-workflow.md`.
