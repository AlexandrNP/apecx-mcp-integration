# Clean-install test-collection audit (2026-05-21)

Investigation + fixes for the 12 pytest **collection** errors a collaborator hit
on a clean install (`pytest -m "not (integration or smoke or slow)"` →
"Interrupted: 12 errors during collection"), plus a sweep for the broader class
of clean-install import landmines.

## Why it "comes down to import failures during collection"

The single most important fact: **pytest collection IS import.** To discover the
test functions in `tests/foo.py`, pytest imports `tests/foo.py` as a module. So:

1. **A module-scope `import X` of an uninstalled `X` raises at collection time**,
   before any test runs. The whole module errors.
2. **Marker filters do NOT save you.** `-m "not integration"` deselects marked
   tests, but deselection happens *after* collection (i.e. after import). An
   `@pytest.mark.integration` module that does `from academy.agent import ...`
   at module scope still fails to import — the marker never gets a chance to
   exclude it.
3. **Collection errors are fatal to the run.** pytest reports them as errors and
   exits non-zero; `make unit` then fails (`Error 2`). One un-importable test
   module takes down the whole suite — even if the other 1700 tests are fine.

This is why "import failures during collection" is the dominant clean-install
failure shape: the test code never even gets to run; it dies at import.

## The dependency baseline

A clean test install is `pip install -e .[dev]` = base deps + the `dev` extra
(pytest, ruff, aiosmtpd, …). It does **NOT** include:

| Extra | Packages |
|---|---|
| `rag` | `sentence-transformers`, `faiss-cpu` |
| `hpc` | `globus-compute-sdk`, `globus-sdk`, `keyring` |
| `academy` | `academy-py` |

`apecx-harvesters` and `nanobrain` ARE base deps (installed from git). So a
module-scope import of anything in `rag`/`hpc`/`academy` breaks collection on a
default test install; a base-dep import only breaks if the installed *version*
lacks a symbol the test imports.

## The 12 errors benc hit — root causes + fixes (commit `4536de8`)

| Count | Module(s) | Root cause | Fix |
|---|---|---|---|
| 10 | `tests/benchmarks/problems/**/test_code.py` | **Eval templates masquerading as test modules.** Named `test_*.py` and living under `tests/`, so pytest collects them — but each is a codegen-candidate template that `assert`s a candidate symbol (`DoubleStep`, `UpperStep`, …) at module scope, expecting a generated candidate to be *prepended* before execution. The bench sandbox (`tests/benchmarks/sandbox.py`) runs them in a subprocess from a string; pytest must never collect them standalone. | `pytest_ignore_collect` hook in `tests/conftest.py` excluding `*/benchmarks/problems/*`. |
| 1 | `test_academy_real_integration.py` | Module-scope `from academy.agent import …` of the optional `academy` extra → `ModuleNotFoundError` at collection (the `@pytest.mark.integration` couldn't help — see #2 above). | `pytest.importorskip("academy.agent")` before the import; docstring's "hard failure not skip" rationale corrected (academy is optional, so a collection abort would punish the majority who don't install it). |
| 1 | `test_harvester_contract.py` | `importorskip("apecx_harvesters")` *passed* (it's a base dep) but the next line imported `Transform`, a symbol the installed `@main` version no longer exposes → `ImportError`. **Version drift**, not a missing package. | Guard the symbol imports in a `try/except ImportError → pytest.skip(allow_module_level=True)`. |

## Broader sweep — other clean-install landmines

I audited **all** of `src/` and `tests/` for the anti-pattern (env-independent —
grep finds it regardless of what my venv has installed):

```bash
grep -rnE "^(import|from) (sentence_transformers|faiss|globus_sdk|globus_compute_sdk|keyring|academy|parsl|proxystore)\b" src/ tests/
```

Findings:

1. **`src/apecx_integration/agents/domain_rag/index.py` (FIXED, commit below).**
   The only module-scope optional import in all of `src/`: it hard-imported
   `sentence_transformers` + `faiss` (the `rag` extra) at module scope. That made
   `index.py` — and `DomainRagSearchStep`, which references `DomainRagIndex` by
   class path — **un-importable** without `.[rag]`, and it quietly undercut the
   G81 "RAG degrades gracefully" contract: that contract only covered a missing
   index *file*, not a missing *package*. It was LATENT (no test or startup path
   imports the step at module scope — the framework imports it lazily by class
   path, and the synthesis steps + MCP server import `DomainRagIndex` inside
   functions), but loading a RAG workflow without `.[rag]` gave a raw
   `ImportError` instead of an actionable message, and the degradation test
   sidestepped the whole thing with a file-level `importorskip`.

   **Fix:** lazy, order-preserving `_import_rag_libs()` (sentence_transformers
   before faiss — the macOS-ARM segfault constraint); `search()` degrades to
   `[]` + a loud `pip install -e '.[rag]'` warning when the extra is absent,
   exactly like a missing index file. Proven: `index.py`, `domain_rag_step.py`,
   and the synthesis step all import with both packages *blocked*; `search()`
   returns `[]` (no crash). The degradation test file no longer `importorskip`s —
   the contract is now tested on a clean install (where it matters), plus a new
   `test_search_degrades_when_rag_packages_missing` covers the package-absent
   branch.

2. **`tests/integration/test_email_notifier.py` imports `aiosmtpd` at module
   scope (LOW risk, left as-is).** `aiosmtpd` is in the `dev` extra — which you
   must have installed to run pytest at all — so it's present whenever the suite
   runs. Not a clean-install blocker. (Noted for completeness; if a future
   contributor runs pytest without `.[dev]`, guard it then.)

3. **Production entry points are clean.** `apecx-mcp` (server.py), `apecx-setup`
   (setup.py), `apecx-globus-setup` (globus_setup.py), `apecx-cp` (app.py) have
   **no** module-scope optional imports — all defer to runtime. A clean
   `pip install -e .` boots them without any extra.

## Prevention guidance (for future contributors / LLM codegen)

- **Optional/extra dependencies (`rag`/`hpc`/`academy`) MUST be imported lazily**
  (inside the function/method that uses them) OR guarded with
  `pytest.importorskip` at the top of any test that needs them. Never import an
  extra at module scope in `src/` or in a test module.
- **A base-dep import can still drift** — if a test imports a *symbol* from a
  base dep, guard the symbol import too (the package present ≠ the symbol
  present).
- **Don't name non-test files `test_*.py` under `tests/`.** The benchmark
  problem templates are scaffolding; they're excluded by a `conftest` hook now,
  but the cleaner long-term move is to rename them (e.g. `problem_check.py`) or
  host them outside `tests/` so pytest never tries to collect them. (Deferred —
  the bench sandbox references them by their current path.)
- **Graceful-degradation must cover the package level, not just data.** "Feature
  X is optional" means importing X's module works *without* X's packages and
  degrades loudly — not that X crashes on import when its extra is absent.

## Commits

- `4536de8` — the 12 collection-error fixes (conftest hook + 2 importorskip/guard).
- (this arc) — `domain_rag/index.py` lazy-import + package-level graceful
  degradation; `test_domain_rag_graceful_degradation.py` de-skipped + new
  package-missing test.
