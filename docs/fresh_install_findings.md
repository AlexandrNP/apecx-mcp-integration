# Fresh-install (packaging / delivery) validation — findings + backlog

Harness: `scripts/validate_fresh_install.py` (builds the wheel, validates the DELIVERED package).
Companion to `docs/workflow_boundary_findings.md` (which validates BEHAVIOR via the editable dev tree).

## Why this loop exists

The boundary loop runs `PYTHONPATH=src` — the editable source tree — so every packaged-resource path
resolves and a **wheel-delivery gap is invisible**. That blind spot shipped a real bug: the PyMOL
build context (`docker/pymol/_pymol_job.py`) lived OUTSIDE `src/` and was absent from the wheel, so a
`uv tool install` user hit `FileNotFoundError` while **every dev test passed**. The loop validated the
product's behavior, never its delivery.

This harness closes that gap: it builds the wheel and asserts the load-bearing, module-relative
resources are actually inside it AND resolve in the install layout, then (in `--full`) does a real
`uv tool install` + `apecx-setup --non-interactive` + the C1-C6 boundary contract from the DELIVERED
venv (not `PYTHONPATH=src`).

## Tiers
- **default (deterministic, no network — CI-safe):** build wheel → delivery manifest → install-layout
  resolution → entry points → package-data globs match.
- **--full (env-gated, heavy):** real `uv tool install` into an isolated dir → `apecx-setup
  --non-interactive` → boundary e2e from the delivered venv. Deps are git URLs (nanobrain,
  apecx-harvesters) so the README install is genuinely resolvable; the dict (~735MB) is skipped via
  `APECX_SKIP_DICT_BUILD=1`.

## The harness's OWN blind spot (caught in review) → the derived delivery gate

The first cut of this harness used a **hand-maintained manifest** of load-bearing resources. The
review-gate correctly flagged that as *false confidence*: a hand-list only catches a REGRESSION of a
resource someone already listed — it would NOT have caught the original PyMOL bug prospectively, and
it printed "ALL CLEAR" while `seqtest.fasta` (a bundled workflow default, read by
`fasta_collection_step`) was absent from the wheel — the IDENTICAL FileNotFoundError class.

Fix: the primary gate is now **derived from the source tree** — `check_all_resources_ship` asserts
that EVERY non-`.py` resource under `apecx_integration/` ships in the wheel (minus a small, explicit,
justified dev-only denylist: `*.example`). Switching to the derived gate immediately surfaced THREE
un-shipped resources the hand-list missed: `composition/workflows/rhea_muscle_alignment/data/seqtest.fasta`,
`_alembic/migrations/script.py.mako`, `_alembic/migrations/README`. All three are now shipped
(pyproject `**/*.fasta` + `_alembic/**/*`); the harness verifies **all 198** non-`.py` resources ship.
Lesson recorded: a packaging loop must DERIVE its contract from the code, never hand-list it.

## Scorecard (this branch — atop the PyMOL packaging fix)
- **default tier: ALL CLEAR.** The delivered wheel carries every load-bearing resource
  (`_pymol_container/{Dockerfile,_pymol_job.py}`, `_pymol_sasa.py`, `composer_config.yml`, alembic
  versions, the viral_epitope_analysis workflow) + all four entry points (apecx-mcp/cp/setup/globus).
- **Regression proof (the bug this loop was built to catch):** the inverted self-test injects the
  PRE-FIX PyMOL location (`docker/pymol/_pymol_job.py`, outside `src/`) into the manifest and the
  harness correctly reports it **MISSING from wheel** — i.e. on `origin/main` before the PyMOL fix
  this loop would have FAILED, exactly the `uv tool install` user's crash.
- Benign: the `**/*.yaml` package-data glob matches nothing (the repo uses `.yml`) — a WARNING, not a
  failure (intentional future-proofing).

## --full / --e2e tier results (real fresh install, recorded)
- `uv tool install --from <checkout>`: **✅** (deps resolve from git: nanobrain @ academy-integration,
  apecx-harvesters @ main).
- `apecx-mcp --help` / `apecx-setup --help`: **✅ rc=0**.
- `apecx-setup --non-interactive` (APECX_SKIP_DICT_BUILD=1): **✅ rc=0**. The DELIVERED package's verify
  shows **no data/violin rows** (the cleanup shipped in the wheel) and every optional honest-skips
  (globus/data/rag/rhea/pymol).
- **`--e2e` (the real end-to-end): workflow ran from the delivered `uv tool` venv → C1 status `ok`,
  all 13 stages.** Decisive: `structural_reasoning available=True, n_exposed=31` — the PyMOL job script
  was FOUND (packaged), the image ran, real SASA computed. **The FileNotFoundError is gone end-to-end
  in a real install** — the exact regression this whole packaging/delivery arc was about. Conservation
  also ran (`available=True, n_conserved_regions=47`). Only degrade: RHEA (`available=None`, honest
  "NOT available" — the optional additive MUSCLE leg; not a crash).
- **Caveat that confirms backlog #1:** the conservation leg ran only because THIS host has the `mafft`
  binary installed. A truly clean env (no host `mafft`) would fail it — exactly why MAFFT must become
  a self-provisioning container (next arc). This harness's e2e on a mafft-less host would surface it.

## Setup-validation cleanup (server-side / no-local-files), shipped this arc
- `cli/setup.py::_step_verify` no longer checks or lists local BV-BRC/VIOLIN CSVs, and the `optional`
  set drops `data`/`violin`. harmonized_search uses the public Globus index anonymously and the
  primary workflow pulls data over the network; the `query_*` DB tools degrade-loud at call time.
- `mcp_surface/server.py` no longer runs the `_check_data_root_or_warn()` startup banner (it told a
  no-local-data install it was "missing" something it does not need — misleading under Globus-first).
- Regression pin: `tests/unit/test_verify_no_local_data.py`.

## Backlog (next, loop-driven)
1. **MAFFT self-provisioning (container-only).** The conservation leg's default aligner is the host
   `mafft` binary (`local_mafft_align_step.py` → `shutil.which('mafft')`) — a SEPARATE manual install,
   the last bio-tool that isn't self-provisioning. Containerize it like PyMOL (the `_pymol_container`
   pattern: packaged `_mafft_container/{Dockerfile,job}` + `ensure_docker_image_built` +
   `container_admission`), so no `brew install mafft` is ever needed; degrade-loud when Docker absent.
   This is the next arc.
2. **Wire the harness into the loop cadence** — run the default tier alongside the boundary loop on
   every packaging-affecting change; run `--full` before a release.
3. Consider extending the manifest as new module-relative resources are added (the manifest is the
   delivery contract; a new `Path(__file__).parent / <data>` resource must be added here + to
   package-data).
