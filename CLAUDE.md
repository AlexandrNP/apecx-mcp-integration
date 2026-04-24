# apecx-mcp-integration — repo-local Claude instructions

Workspace-root `../CLAUDE.md` carries the cross-repo rules (git
discipline, mocks policy, nanobrain framework constraints, session
distillation directive). This file carries repo-specific details
that a fresh Claude session needs to skip friction already paid for.

## Python interpreter — use the venv

**`.venv/bin/python` is authoritative. `/opt/anaconda3/bin/python`
is NOT.** The project's editable installs live in the venv:

- `apecx_integration`        (this repo)
- `apecx_db_integration`     (sibling repo `../apecx-db-integration`)
- `nanobrain`                (sibling repo `../nanobrain`)

Running `python -m pytest ...` from the shell picks up whichever
interpreter is first on `PATH`. On this laptop that is the system
anaconda, which does not see any of the editable installs. Symptoms:
`ModuleNotFoundError: No module named 'apecx_db_integration'` or
`... 'nanobrain'` on a test file that clearly imports them.

**Always invoke pytest via the venv explicitly:**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/...
```

See `docs/session_friction_log.md` #14.

## Live-LLM test recipe

Three test files are gated on a reachable Ollama (auto-skip when it
isn't). Recipe:

```bash
# Ollama must be running + mistral-nemo:latest pulled.
APECX_LLM_BASE_URL=http://localhost:11434/v1 \
APECX_LLM_MODEL=mistral-nemo:latest \
APECX_LLM_TEMPERATURE=0.0 \
APECX_LLM_MAX_TOKENS=2048 \
APECX_LLM_API_KEY=unused \
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/integration/test_composer_phase2_against_ollama.py \
  tests/integration/test_composer_ac7_composition_bias.py \
  tests/integration/test_t01_ac1_against_ollama.py \
  tests/integration/test_composer_ac8_walltime.py -v
```

The env vars override `composer_config.yml` at load time — see
`src/apecx_integration/composition/composer.py::_apply_llm_env_overrides`.

## Composer prompt engineering is load-bearing

`src/apecx_integration/composition/composer_prompts/system.md` is
not documentation — it's the file that makes T01 AC1 pass or fail.
Two drift patterns broke AC1 during development (2026-04-22) and
are now explicitly constrained:

1. **No TransformLink.** LLMs hallucinate `transform_function`
   import paths. The prompt forbids TransformLink; use DirectLink
   + novel Python when shape-bridging is required.
2. **Path-reference `config:` for library components.** Inline
   `config: {...}` forces the LLM to reproduce `input_data_units`
   / `output_data_units` / `triggers` blocks and hallucinate their
   class paths. The prompt mandates `config: "<wrapper_yaml>"`.

If AC1 starts flapping again, check this file BEFORE blaming the
LLM or the executor.

## FAISS / sentence-transformers import order

`nanobrain/nanobrain/lightweight/component_index.py` imports
`sentence_transformers` BEFORE `faiss`. Load-bearing — reversing
the order causes a silent segfault on macOS ARM. The file carries
a `# ruff: noqa: I001, E402` + a comment explaining why. Do not
let an auto-sort "fix" that. See `docs/session_friction_log.md` #13.

## RAG index build

The composer's RAG backend loads from `rag_index_dir` if set. Build
the index out-of-band:

```bash
PYTHONPATH=../nanobrain:src .venv/bin/python \
  scripts/build_rag_index.py \
  src/apecx_integration/composition/composer_config.yml
```

Output is `<config_dir>/rag_index/{faiss.bin,metadata.json}` by
default (override with `--out`). Without a built index, the
composer falls back to the Phase-2 linear-scan ComponentCatalog.

## Key reference docs

- `../architectural_plan.md` — project-level source of truth.
- `../implementation_plan.md` — task table + scoreboard.
- `docs/composer_task_spec.md` — T-COMP phased delivery + ACs.
- `docs/workflow_spec.md` — the VIOLIN × BV-BRC workflow definition.
- `docs/session_friction_log.md` — what burned time before.
- `docs/nanobrain_mock_audit.md` — T14 audit + fix rows.
