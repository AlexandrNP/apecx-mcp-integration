# Session friction log — recurring time sinks + mitigations

Living inventory of the issues that eat real minutes across Claude
Code sessions on this workspace. Each entry has a **measured cost**
(not "seems slow"), a **root cause**, a **mitigation already shipped**
(or the reason it hasn't), and a **detection signal** so future
sessions catch the same issue faster than this one did.

Not a rant log. If an entry here can't be reduced to a one-line fix
or a concrete "here's how to recognize this faster" rule, it belongs
elsewhere (scope-decisions, future_work).

---

## 1. Full-suite pytest hangs when Ollama is reachable

**Cost this session alone:** ~10–15 min across three turns (the
2026-04-22 Task-1 finish, 2026-04-23 T13 lint-and-test pass,
2026-04-23 T-COMP P1 commit).

**Root cause:** `tests/integration/test_t01_vertical_slice_against_ollama.py`
and `tests/integration/test_violin_bvbrc_workflow_against_ollama.py`
each carry `@pytest.mark.skipif` decorators that skip when Ollama is
**not reachable** or the target model isn't pulled. On a workstation
with Ollama running and `mistral-nemo:latest` already pulled, the
skip doesn't fire and the test runs LIVE. Some tests also check
`APECX_DB_DATA_DIR` + Control-Plane reachability, but any one
skipif passing turns the test into a live-LLM round-trip that
takes minutes.

**Why it's particularly annoying:** neither test file emits any
"about to make a live LLM call" warning. Pytest just stops printing
dots for 60–120s per hanging test. Hard to distinguish from a true
hang without `ps aux | grep ollama`.

**Mitigation shipped (commit ``<pending>``):** added an
``APECX_SKIP_LIVE_LLM`` env-var opt-out as an ADDITIONAL skipif
clause on every Ollama-gated test in the three files:

- ``tests/integration/test_t01_vertical_slice_against_ollama.py``
- ``tests/integration/test_violin_bvbrc_workflow_against_ollama.py``
- ``tests/integration/test_nanobrain_agent_against_ollama.py``

When set to ``"1"``, these tests skip regardless of Ollama state.
Claude sessions should set it; operators leave it unset. Verified
with the 3-file subset: 2 passed, 5 skipped in 1.96s under the env var.

**Incomplete mitigation — follow-up bug flagged 2026-04-23:** even
with the env var set, ``pytest tests/ --ignore=tests/integration/hpc``
still hangs in a different place (not Ollama). Stall observed around
the 105–110-test mark; CPU time plateaus near 8s. Suspected culprit:
a Docker / Apptainer / Control-Plane-lifecycle test that spawns a
container or binds a socket and doesn't honor its own timeout. Not
re-investigated this turn; pragmatic workaround is to run the
composition subset (154 tests, ~30s) instead of the full suite until
the second hang is diagnosed. Filed as a TODO in this log; needs
someone to bisect `pytest --collect-only` against ``tests/integration/``
file-by-file and find the stall site.

**Detection signal for future sessions:** if pytest CPU time plateaus
(``ps -p <pid>`` shows flat `TIME`), check for Ollama runners:
``ps aux | grep "ollama runner"`` — spawn time near the pytest start
== smoking gun. If no Ollama runner is spawned but pytest still
stalls, it's the non-Ollama hang described above.

---

## 2. Pipe buffering (`| tail -N`) masks pytest progress

**Cost this session:** ~10 min across two false-alarm kills of
pytest mid-run.

**Root cause:** `pytest ... 2>&1 | tail -3` buffers pytest's stdout
until the producer closes. `tail` only emits once EOF arrives. If
the producer (pytest) takes 2 min, the output file stays 0 bytes
for 2 min — looks exactly like a hang to a caller checking file
size.

**Mitigation (behavioral, not code):** use one of:

- ``tee /tmp/run.log`` + a `Monitor` call with
  ``tail -F /tmp/run.log | grep --line-buffered ...`` so progress
  streams.
- Plain `run_in_background: true` on the full `pytest` invocation
  (no `| tail`), then read the output file after completion.
- Run without background, accept the wait.

**Detection signal:** 0-byte output file AND pytest CPU time still
increasing (``ps -p <pid>`` shows `TIME` going up). That's
progressing under the pipe, not hanging. Don't kill.

---

## 3. Harness cwd persists through failed chains

**Cost this session:** ~5 min across two wrong-cwd pytest runs.

**Root cause:** `cd foo && cmd` persists the `cd` in the Bash
tool's working-dir state even when `cmd` fails. The tool's
"working directory persists between commands" contract doesn't
unwind on failure.

**Manifestation in this session:** earlier in the turn, a
``cd wt-X && broken_python_path -m ruff ...`` call failed. Cwd was
already updated to wt-X. The next command's relative paths resolved
against wt-X instead of the caller's intended directory.

**Mitigation (behavioral):** use absolute paths for everything
cross-worktree. Or explicitly `pwd` at the start of a new chain to
confirm where the shell actually is.

**Detection signal:** ruff / pytest report
``E902 No such file or directory`` on a file that clearly exists.
The file exists in the INTENDED dir, not the ACTUAL cwd.

---

## 4. Edit tool reports success but the change doesn't stick

**Cost this session:** ~10 min recovering a silently-lost test in
T04.

**Root cause:** two Edit calls targeting the same file. The second's
``old_string`` matched the first's ``new_string``. Both reported
"file has been updated successfully." But when collected later, the
file content didn't reflect either edit — the second Edit's
`new_string` apparently replaced the first's output but also lost
the first's addition. Or some interaction I never fully traced.

**Mitigation (behavioral):** after a batch of Edits to the same
file, run a quick ``grep -n <marker>`` for the new content. If it's
missing, the Edit didn't stick — don't trust the success message.
Test collection (``pytest --co -q``) is an even stronger check
because it catches imports / symbols that no longer exist.

**Detection signal:** a test that names a function or class fails
collection with ``AttributeError`` or ``ModuleNotFoundError`` for a
symbol you just added. The Edit didn't land; the symbol doesn't
exist.

---

## 5. Implementation_plan.md status drift

**Cost this session:** ~5 min discovering T11 was already shipped
AFTER I'd started authoring a duplicate.

**Root cause:** the workspace-root `implementation_plan.md` is not
git-tracked (workspace root isn't a git repo). Status updates
accumulate on disk but can't be reverted, forked, or diffed against
a baseline. The source-of-truth for "is X shipped?" is code, not
the status table.

**Mitigation (behavioral):** before starting any task that's
marked ❌ in the status table, grep for existing code at the task's
named file path. The audit table's last-audited date is load-bearing
— older than 2 days = treat as a hypothesis, not fact.

**Detection signal:** file at the task's documented path already
exists. Grep it before coding.

---

## 6. Nanobrain is ungit'd — on-disk changes have no rollback

**Cost this session:** 0 minutes so far, but growing risk.

**Root cause:** `/Users/onarykov/Downloads/apecx-cowork/nanobrain/`
is not a git repository. The TransformLink fix (2026-04-23) lives
as uncommitted on-disk edits. If someone re-fetches nanobrain from
its upstream, my changes get overwritten silently.

**Mitigation (not yet taken):** either `git init` the nanobrain
directory or extract pending changes as a patch file. User-level
scope decision; not in my authority.

**Detection signal:** apecx-mcp-integration tests that rely on the
nanobrain fix (e.g., `test_transforms_with_transformlink.py`) start
failing with "TransformLink has no from_config" or similar — means
nanobrain got reverted under us.

---

## 7. Docstring promises out-of-scope from the implementation

**Cost this session:** ~3 min on the TransformLink `pkg.mod.Class.method`
resolver — docstring claimed deep attribute walking; first
implementation only did `rpartition`. Test caught it; fix was a
for-loop backward walk.

**Root cause:** writing the spec / docstring before the code, and
over-promising capability. Not a harness issue — a self-discipline
issue.

**Mitigation (behavioral):** when writing a docstring before the
code, treat the docstring as a test requirement. If the first-cut
implementation doesn't satisfy the docstring's claim, FIX THE CODE,
don't weaken the docstring. Docstrings rot less than tests; they
shouldn't be retrofitted.

**Detection signal:** reader's ``example`` in a docstring doesn't
actually run — that's the test that should have existed.

---

## 8. Workspace-root edits don't participate in any git history

**Cost this session:** 0 minutes (yet), but the implementation_plan
status-drift problem in #5 is a manifestation of this.

**Root cause:** files at
`/Users/onarykov/Downloads/apecx-cowork/*.md` (architectural_plan,
implementation_plan, CLAUDE.md, etc.) are shared across all sibling
repos but tracked by none of them. Edits persist on disk but
aren't in any repo's log.

**Partial mitigation:** commit messages in `apecx-mcp-integration`
that touch implementation_plan.md include a "workspace-root update
(non-git)" note so the commit history at least REFERENCES the edit.

**Systemic fix:** make the workspace root a git repo, or move these
files into one of the sibling repos. Out of scope for me; user
decision.

---

## 9. Scaffold-vs-stub distinction blurs across sessions

**Cost this session:** 0 minutes, but I've explicitly flagged it
twice as a pattern to preserve. Recording so future sessions don't
collapse the distinction.

**The rule:** a **scaffold** fails loudly (``NotImplementedError``
citing the next phase's spec) and is documented. A **stub** pretends
to succeed (returns empty dict, silently does nothing). Scaffolds
are legitimate; stubs are the "looks-wired-but-doesn't-work" failure
mode workspace CLAUDE.md is built to prevent.

**Detection signal:** if the function body has a ``return {}``,
``return None``, or ``pass`` AND there's no raised exception — it's
a stub, not a scaffold. Either make it raise or make it do the work.

---

## How to add to this log

- Only entries that ate ≥3 min or recurred across turns.
- Each entry needs: cost, cause, mitigation, detection signal.
- Delete entries when the mitigation is durable and the detection
  signal stops firing for a week.
- If you're debating whether to add an entry, the friction was
  probably smaller than it felt. Don't add it.
