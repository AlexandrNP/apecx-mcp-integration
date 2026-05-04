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

**(none)**

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
