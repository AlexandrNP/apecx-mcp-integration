# TODO

Bite-sized follow-ups from the install/setup work that landed across
PRs #2 → #14 (2026-04-27/28).  Each item lists **what**, **why
deferred**, and **trigger to revisit**.  Larger initiatives have
fuller writeups in the workspace-config repo's
`apecx-mcp-integration_dev_history/future_work.md` — referenced by
relative path below.

This file is intentionally short.  When an item ships, delete it.
When it grows past three bullets, promote it into the dev-history
`future_work.md` or its own design doc.

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

**Full proposal:** `../_workspace_notes/apecx-mcp-integration_dev_history/future_work.md`
— section "`APECX_LLM_API_KEY` plaintext storage in Claude Desktop
config".  Sized 1.5–2d.

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
`../_workspace_notes/apecx-mcp-integration_dev_history/session_friction_log.md`
with the detection signal ("test count from `pytest -v` doesn't
match the number of `def test_` lines in the file under test") and
the fix (`--rootdir=.`).  Note: friction log is now unversioned
(see workspace `_workspace_notes/README.md`).

## 5. Workspace-root files are unversioned (CLAUDE.md, settings, _workspace_notes)

**What:** Three workspace-root artifacts shape every Claude Code
session AND every `git`/install operation, but live in
`/Users/<you>/Downloads/apecx-cowork/` which is not a git repo:

- `CLAUDE.md` — workspace-wide rules loaded into every session.
  Edited four times this session (PRs #11, #13, plus inline edits
  for friction-log path updates).
- `.claude/settings.json` — harness permission deny-list.  Edited
  in PR #11 to close the `git checkout -B` gap.  Active behavioral
  change with no version control.
- `_workspace_notes/` — created in PR #13.  Contains the friction
  log (entry #22 added today), 13 historical dev docs, and the
  `agentic/` scratch dir.

These are critical-to-process and lose ALL of:
- git history (no `git log`, no `git blame` on individual entries),
- visibility on PRs (changes don't show up in any review),
- portability (a fresh laptop doesn't have them; another collaborator
  has whatever-state-they-last-pulled),
- backup (only via Time Machine / Dropbox / whatever's running).

The friction log is the worst case: it was actively being appended
to as a versioned artifact for weeks, and is now unversioned going
forward.  Same for the deny-list (a future hook gap will need to
be discovered all over again).

**Why deferred:** the cleanup PR (#13) was about reducing repo
docs/ noise, not about restructuring the workspace itself.
Bundling versioning would have ballooned scope.

**Trigger to revisit:** any of these conditions, in roughly
priority order:
1. Second developer joins and needs to pick up the workspace
   conventions (their copy is naively different from yours).
2. Disk failure / OS reinstall of the current machine.
3. Friction log entry #25 gets written, demonstrating that the
   "unversioned" status hasn't slowed accretion (and so the loss
   risk is non-trivial).
4. Workspace `CLAUDE.md` size approaches the always-loaded
   context budget — at which point splitting/refactoring it is
   risky without `git log` to undo.

**Two solution sketches** (not implementations):

**A. Make workspace root itself a git repo.**
   - `cd /Users/<you>/Downloads/apecx-cowork && git init`.
   - Track `CLAUDE.md`, `.claude/`, `_workspace_notes/` (and
     anything else workspace-meta).
   - `.gitignore` the sibling repos (`apecx-mcp-integration/`,
     `nanobrain/`, etc.) since they're independent clones with
     their own histories.
   - Push to a new GitHub repo (e.g., `AlexandrNP/apecx-workspace`).
   - **Risk:** the workspace CLAUDE.md says "the workspace root
     itself is not a git repo" — this is a deliberate prior
     decision the user made and should re-decide before reversing.
   - **Cost:** ~30 min to init + ignore sibling repos + push.

**B. Sibling versioned-config repo.**
   - Create `apecx-workspace-config` as a sibling repo containing
     just the meta files.
   - Symlink them into the workspace root: `ln -s
     ../apecx-workspace-config/CLAUDE.md /workspace/CLAUDE.md`.
   - Push to GitHub like the others.
   - **Risk:** symlinks break on Windows (some setups), and the
     symlink-into-workspace-root pattern is non-obvious to a
     fresh operator landing in the workspace.
   - **Cost:** ~1 hour, more moving parts than A.

Recommendation if/when this gets done: **A**, with a short README
at the workspace root explaining the structure to a new operator.

**Cost estimate:** 0.5–1 day depending on how much existing state
lands in the new repo and whether you want a CI hook to enforce
"no plaintext secrets in workspace config" or similar.
