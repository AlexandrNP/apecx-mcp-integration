# TODO

Bite-sized follow-ups from the install/setup work that landed across
PRs #2 → #9 (2026-04-27/28).  Each item lists **what**, **why
deferred**, and **trigger to revisit**.  Larger initiatives have
fuller writeups in `docs/future_work.md` — those are cross-linked
here, not duplicated.

This file is intentionally short.  When an item ships, delete it.
When it grows past three bullets, promote it into `docs/future_work.md`
or its own design doc.

---

## 1. Move `APECX_LLM_API_KEY` out of plaintext (keyring)

**What:** Real cloud LLM keys (Anthropic, OpenAI) currently land in
`claude_desktop_config.json` in plaintext.  Captured by Time Machine,
Dropbox, and any home-dir backup.  See "Secrets handling" in
`docs/mcp_integration.md`.

**Why deferred:** non-trivial cross-OS work (macOS Keychain prompts
on first access, Linux libsecret backends vary, Windows is generally
clean).  Bundling into the install-UX PRs would have inflated diff
size.

**Trigger to revisit:** first operator request for paid-cloud LLM in
production, OR first `claude_desktop_config.json` accidentally
committed to a git repo.

**Full proposal:** `docs/future_work.md` — section
"`APECX_LLM_API_KEY` plaintext storage in Claude Desktop config".
Sized 1.5–2d.

## 2. LLM-reachability startup gate in `apecx-mcp`

**What:** Mirror the data-root startup banner (added in PR #5) for
the LLM endpoint.  When `apecx-mcp` starts, after the Control Plane
healthcheck, do a one-shot probe of `APECX_LLM_BASE_URL` (`GET
/models` or `/v1/models`).  On failure, log a multi-line WARNING
banner pointing at `apecx-setup --reconfigure-llm`.

**Why deferred:** different LLM endpoints have slightly different
discovery semantics (Ollama: `/api/tags`; OpenAI: `/v1/models`;
vLLM: usually `/v1/models`).  Picking one probe path that doesn't
false-alarm across all of them is real work.  Until one operator
hits "tools work but composer fails silently," the missing gate is
theoretical.

**Trigger to revisit:** first operator report of "the database
tools work but `start_workflow` silently fails."

**Cost estimate:** 0.5d if we accept a short timeout on `/v1/models`
+ a fallback to `/api/tags` for Ollama specifically; longer if we
want the probe to be exhaustive.

## 3. Integration test for `apecx-setup` install pipeline

**What:** No end-to-end test exists that exercises a real `gh
release download` against the private `apecx-data` repo + extract +
config patch.  The unit suite (36 tests) covers everything *except*
the actual cross-process boundary that already broke once during
development (the "push succeeded but repo shows empty" git buffer
issue, fixed by `http.postBuffer 524288000`).

**Why deferred:** live integration tests for a private-repo download
need (a) `gh` auth on the test runner, (b) opt-in env-var gate so
CI doesn't burn rate-limit, (c) isolated tmp data dir.  Achievable
but out-of-scope for the install-UX PRs.

**Trigger to revisit:** any future change to the download path
(`_download_asset`, the tarball format, the release tag scheme),
OR a bug report that "the install instructions don't work on a
clean machine."

**Sketch:** new file `tests/integration/test_apecx_setup_pipeline.py`
gated on `APECX_SETUP_INTEGRATION=1` + `gh auth status` returncode
0.  Calls `_download_asset` against a temp dir, asserts the tarball
shape, runs the extract step, asserts the file count.  ~0.5d of work.

## 4. Friction-log entry: pytest rootdir trap in worktrees

**What:** When running `pytest` from a git worktree whose `.venv`
is symlinked to the main checkout's venv, pytest's rootdir
auto-discovery follows the symlink and uses the *main* checkout as
rootdir.  Result: pytest collects tests from the main checkout's
`tests/` tree, not the worktree's — silently skipping new tests
written in the worktree.  Workaround: pass `--rootdir=.` explicitly.

**Why deferred:** documentation, not code.  The workaround is
two characters; the trap is "I added new tests but they don't
appear in the run."

**Trigger to revisit:** N/A — just write the friction-log entry.

**Cost estimate:** 10 minutes.  Add an entry to
`docs/session_friction_log.md` with the detection signal ("test
count from `pytest -v` doesn't match the number of `def test_` lines
in the file under test") and the fix (`--rootdir=.`).
