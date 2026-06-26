# Integration-test parity TODO

Per workspace `CLAUDE.md` **Unit-mock / integration-test parity rule
(2026-04-21)**:

> When authoring a unit test with a mock, record in the test docstring
> (or a sibling comment) which integration test covers the same code
> path. When the integration test is missing, create a TODO in
> `tests/integration/TODO.md` and file it as a T-ticket.

This file is the sink for those TODOs. An empty list is the target
steady state.

## Current gaps

> **T-2026-06-17-01** (`tests/integration/test_structural_reasoning_pymol.py`)
> — The PyMOL surface-visualization render path (`docker/pymol/_pymol_job.py`
> `cmd.png(ray=1)` → PNG → `structural_reasoning_step.py` copies it to the
> artifacts dir as `visualization_artifact`) has NO assertion. The gated test
> (`APECX_PYMOL_DOCKER=1`) now exercises the render because `render_png` is
> always set, but it asserts only the SASA result, not that a PNG comes back.
> Needed: add an assertion that on a real CHIKV run the result carries a
> `visualization_artifact` basename AND the file exists in `_artifacts_dir()`.
> Scope: ~10 lines in the existing gated test; requires the PyMOL Docker image
> with working headless `ray=1` (GL libs). Deferred under the degrade-loud
> design — the render is additive/best-effort and the SASA correctness path is
> already covered.

> **T-2026-06-26-01** (`tests/unit/test_structural_reasoning_step.py::test_docker_available_pulls_when_image_absent_but_daemon_up`)
> — The #3 probe fix's absent→pull→SUCCESS branch (`_docker_available` issues `docker pull` when
> the image is absent and returns True on a 0 exit) is verified ONLY by a monkeypatched
> `subprocess.run` unit test. The absent→pull→FAIL branch now HAS real-Docker parity
> (`test_structural_reasoning_pymol.py::test_docker_available_false_for_unpullable_image_real_docker`),
> but the SUCCESS branch has none — a real test needs a controlled `docker rmi <pinned> && pull`
> that would churn the dev Docker cache (the pinned PyMOL image is large). Needed: a gated test
> that pulls a TINY throwaway image (e.g. `hello-world`) from a clean state and asserts True.
> Scope: ~8 lines, gated on the docker daemon being up.

---

### CLOSED: T-2026-04-23-03 — PubMed real API integration (closed 2026-05-04)

Implemented NCBI E-utils pipeline (esearch → efetch → XML parse) in
`nanobrain/nanobrain/library/tools/bioinformatics/pubmed_client.py`.
Covered by `tests/integration/test_pubmed_live.py` (APECX_PUBMED_LIVE=1).
6/6 live tests pass against real NCBI API.  Mock-policy sentinel in
`test_nanobrain_mocks_policy.py` updated from "raises NotImplementedError"
to "is a coroutine + source has no NotImplementedError."

---

The current unit suite has three files with mock-like constructs, all
of which are already covered by a real-backend integration test:

| Unit test | Mock shape | Covered by |
|---|---|---|
| `tests/unit/test_api_routes.py` | `TestClient(create_app())` against 501-stub routes — ASGI transport, not a network mock | `tests/integration/test_api_approvals.py`, `tests/integration/test_api_status.py`, `tests/integration/test_client_happy_paths.py` (live FastAPI against real SQLite) |
| `tests/unit/test_control_plane_client.py` | `httpx.ASGITransport` shim over the real FastAPI app; only asserts `NotImplementedError` behavior for endpoints still stubbed | `tests/integration/test_client_happy_paths.py` exercises the same client end-to-end for the now-real endpoints |
| `tests/unit/test_apptainer_commands.py` | Pure argv-construction assertions; no mocks of external services | `tests/integration/test_apptainer_runtime.py` (live Apptainer via Lima) |

No `unittest.mock` / `MagicMock` / `patch` usage anywhere in the suite
at the time of writing. The `no-ungated-mocks-in-src` pre-commit hook
keeps production code mock-free; its tests scope is handled by the
parity rule.

## How to file a new TODO

Add a row below when you land a unit test that needs a matching
integration test that doesn't exist yet. Format:

> **T-YYYY-NN** (`path/to/unit_test.py::test_name`) — what the unit
> test mocks, what integration test is needed, rough scope. Assign /
> link to an issue tracker as it exists.

## How to close a TODO

When the matching integration test lands, remove the row and add a
line to the appropriate unit test's docstring:

> `Covered by tests/integration/<file>.py::<test>` (per parity rule)
