# Scope Decision 03 — Fix nanobrain packaging

**Date:** 2026-04-21
**Status:** Draft — awaiting user sign-off.
**Triggered by:** T00.2 spike surfaced two concrete packaging bugs in `nanobrain/`.
**Related:** Scope-decision memo 01 (Option C), memo 02 (ApprovalStep in nanobrain — which depends on this).

---

## The problem

From the T00.2 spike (see `docs/spikes/async_pause_resume.md` §3.3):

1. **`pip install -e /path/to/nanobrain` fails** with:
   > others must be specified via the equivalent attribute in `setup.py`

   This is a setuptools error about `pyproject.toml` fields that require a parallel `setup.py` declaration (or a build-system config fix).

2. **Importing nanobrain raises `ModuleNotFoundError: No module named 'aiofiles'`.** The package uses `aiofiles` but does not declare it as a dependency.

**Consequence:** `apecx-mcp-integration` cannot `pip install nanobrain` cleanly. Our spikes use `sys.path.insert(...)`, which is acceptable for throwaway prototypes but NOT acceptable for shipped integration code. Without this fix, every apecx-mcp-integration consumer has to manage their own nanobrain path manipulation.

---

## The ask

Fix two files in `nanobrain/`:

### File 1: `nanobrain/pyproject.toml`

Adjust the `[project]` section (and `[build-system]` if needed) so that `pip install -e .` succeeds without setuptools's "others must be specified" error. The exact fix depends on the current file's content; the most common shape is:

- Move any metadata fields that setuptools insists live in `setup.py` into `[project]` in `pyproject.toml`, OR
- Add a minimal `setup.py` that defers to `pyproject.toml` via `setup()` with no args.

### File 2: `nanobrain/pyproject.toml` (dependencies)

Add `aiofiles` to the declared dependencies. Verify there are no other undeclared imports by grepping for other `ModuleNotFoundError` candidates.

### Verification

```bash
cd /tmp && python -m venv /tmp/.nbcheck
source /tmp/.nbcheck/bin/activate
pip install -e /Users/onarykov/Downloads/apecx-cowork/nanobrain
python -c "import nanobrain.core.executor as e; print(e.LocalExecutor)"
```

The last line must print `<class 'nanobrain.core.executor.LocalExecutor'>` (or similar). If any `ModuleNotFoundError` fires, add the missing dep.

---

## Blast radius

Low. These changes:
- Do not touch any runtime logic.
- Do not change any public API.
- Affect only how `nanobrain/` is installed by downstream projects.

**Rollback:** `git revert` the commit. Trivial.

---

## Rationale for why this is worth a memo (and a nanobrain edit) at all

The alternative is "let apecx-mcp-integration vendor nanobrain as a sys.path insertion." That works for a laptop demo but fails the moment we:
- Ship to an HPC system where we don't control the Python environment.
- Try to run CI in a clean venv.
- Hand the repo to a new engineer who doesn't know the workspace path conventions.

The packaging fix is a one-day chore with permanent value. The sys.path workaround is a forever-tax on every downstream consumer.

---

## Dependencies

- Scope-decision 01 (Option C) — approved. This is another case-by-case nanobrain edit.
- User sign-off on this memo.

---

## Sign-off

- [ ] User approves edit to `nanobrain/pyproject.toml` to fix both issues.
- [ ] User confirms no other undeclared deps are expected.

Agent: Claude Code agent, 2026-04-21.
