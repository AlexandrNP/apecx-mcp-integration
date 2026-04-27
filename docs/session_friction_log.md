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

## 10. Verify source-of-truth branch before editing a framework file

**Cost this session:** ~1 min (user caught it before any wrong code
landed); 0 min of actual bad editing. Durable risk: ~hours if it
ever bites.

**Root cause:** started editing `nanobrain/core/a2a_support.py` and
`academy_integration.py` without verifying which branch / state the
nanobrain directory was on. The prior-session assumption was
"nanobrain isn't a git repo, so there's nothing to check" — correct
for that directory, but the user asked "did you check the
academy-integration branch first?" and that was a real question.
Turned out there are multiple nanobrain clones on disk under
`/Users/onarykov/git/nanobrain*/`, some of them git-tracked with
feature branches. The in-workspace copy happened to be the one I
should edit, but I hadn't verified.

**Mitigation (behavioral):** before any edit to a framework file
(nanobrain/ or similar read-mostly repo):

- ``git rev-parse --git-dir`` — is this a git repo?
- If yes: ``git branch --show-current`` + ``git log -1 --oneline``.
- ``find ~ -maxdepth 6 -name "<repo-name>" -type d`` — are there
  other clones? Which is canonical?

**Detection signal:** user says "check the X branch first" OR the
framework directory might be a clone/copy of an upstream repo. One
minute of verification beats the "edited the wrong thing for an hour"
outcome.

---

## 11. Merge runs from main repo's working tree, not from the worktree

**Cost this session:** ~2 min on the T14 audit merge, debugging
"Already up to date." Recurrence: this is the SECOND session where
I've tripped this.

**Root cause:** `git worktree add ../wt-X -b branch-X main` creates
the worktree at `../wt-X` with HEAD on `branch-X`. If I stay in
that worktree's cwd and run `git merge branch-X`, git sees HEAD is
already `branch-X`, so there's nothing to merge — exits clean with
"Already up to date." but doesn't create the merge commit I wanted
on main.

**Mitigation (behavioral):** `cd ../apecx-mcp-integration` BEFORE
the `git merge` invocation. Documented in
`.claude/agents/git-worktree-guardian.agent.md` standard flow.

**Detection signal:** `git log --oneline --graph -4` after the merge
doesn't show the expected new merge commit. Or the merge output says
"Already up to date." when you wanted a merge bubble.

---

## 12. Hook scripts can fail silently

**Cost this session:** 0 min of visible failure, but ~weeks of
silent failure across prior sessions — the PostToolUse review hook
never produced a single `system-reminder` despite firing after every
Edit. Only caught when the user asked about failing hooks.

**Root cause:** `.claude/scripts/review_on_change.sh` used `mapfile`
(bash 4+ builtin) which doesn't exist on macOS bash 3.2. Under
``set -uo pipefail`` the mapfile failure plus subsequent
unbound-variable accesses exit with rc=1. But rc=1 isn't rc=2 — the
PostToolUse convention is "exit 2 surfaces stderr to Claude as a
system-reminder." Exit 1 was silently swallowed.

**Mitigation (shipped):** rewrote the mapfile call as a
``while IFS= read`` loop. Bash 3-compatible.

**Detection signal:** hooks have not produced any feedback in a
session that did many Edits. Run the hook manually:

    CLAUDE_FILE_PATH=<some file> bash .claude/scripts/<hook>.sh
    echo "exit=$?"

If it errors or exits non-zero on a clean file, the hook is broken.

**Meta-lesson:** hooks configured in `.claude/settings.json` and
pre-commit hooks in `.pre-commit-config.yaml` should be tested at
session start if you rely on them for feedback. A "hook health
check" at the top of a new session would have caught this in under
a minute.

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

## 13. `faiss-cpu` + `sentence-transformers` import order segfaults silently on macOS ARM

**Cost this session:** ~15 minutes (2026-04-22). Smoke test died
immediately after "Load pretrained SentenceTransformer" log line
with only a "resource_tracker: leaked semaphore" warning on exit —
no traceback, no segfault message.

**Cause:** both `faiss-cpu` and `torch`/`sentence-transformers` link
their own copy of libomp (OpenMP). When `faiss` is imported first,
its runtime wins the symbol table; `torch` then runs against a
runtime it wasn't built for and `SentenceTransformer.encode()`
silently segfaults. Isolated repro: the order
`import faiss; from sentence_transformers import SentenceTransformer;
m.encode([...])` kills the process; swapping the import order fixes
it. Device is `cpu` (MPS/CUDA surface different failures).

**Detection signal:**
- Process dies without traceback after `SentenceTransformer` init
  log line.
- Exit shows `resource_tracker: There appear to be 1 leaked
  semaphore objects to clean up at shutdown`.
- Repro works in isolation (one-liner) but fails in your real module.

**Mitigation:**
- Import `sentence_transformers` FIRST in any module that touches
  both libraries. Add a load-bearing comment + file-level
  `# ruff: noqa: I001, E402` so an auto-sort doesn't "tidy" it back.
- Example: `nanobrain/nanobrain/lightweight/component_index.py`
  lines 1-20.

**Source:** 2026-04-22, T03 ComponentIndex bootstrap.

---

## 14. "Python not found" really means "wrong Python"

**Cost this session:** ~5 minutes of false-confidence ("T01 P2 is
blocked!") before noticing the project has a venv with
``apecx_db_integration`` installed editable. The blocker I declared
was imaginary — I was using ``/opt/anaconda3/bin/python``, not
``apecx-mcp-integration/.venv/bin/python``.

**Cause:** Bash invocations default to the first ``python`` on
``PATH``. On this laptop that's the anaconda system Python, which
has a different site-packages than the project's ``.venv``. The
project pins ``apecx_db_integration``, ``nanobrain``, ``apecx_integration``
as editable installs into the venv — none are on the system Python.

**Detection signal:**
- ``ModuleNotFoundError`` on a module that obviously exists in the
  repo (e.g. ``apecx_db_integration`` which is right next door at
  ``../apecx-db-integration``).
- ``which python`` points at ``/opt/anaconda3`` or similar system
  path, not ``<repo>/.venv/bin/python``.
- Test suite's rootdir is correct but collection fails at import.

**Mitigation:**
- Run the project's Python explicitly:
  ``/path/to/apecx-mcp-integration/.venv/bin/python -m pytest ...``
- For pytest invocations, prefer ``python -m pytest`` over bare
  ``pytest`` so the Python interpreter is explicit in the command.
- Before declaring an env dep "not installed", check the venv
  FIRST: ``.venv/bin/python -c "import <mod>; print(<mod>.__file__)"``.

**Source:** 2026-04-22, T01 P2 LocalExecutor bootstrap.

---

## 15. Don't `--ignore=` a test after one failure — run it under the venv first

**Cost this session:** unknown number of merges' worth of
opportunity cost. Six separate tests spent the session being
``--ignore=``-ed because I saw ONE collection error early on
(under system Python, not the venv — friction #14) and cargo-culted
the ignore list from there. Running the full suite under the venv
at end-of-session: every ignored test passed. The "env-drift"
blockers were imaginary; the real ignore list should have been
empty.

**Detection signal:**
- You're writing ``--ignore=<path>`` pytest flags.
- You last ran the ignored tests under system conda Python, not
  ``.venv/bin/python``.
- You haven't retried since friction log #14 (venv vs system
  Python) was distilled.

**Mitigation:**
- Before adding ``--ignore=``, run the test under the canonical
  runner (``scripts/run_tests.sh <path>``) and paste the failure
  output into the commit message or your session notes. No
  justification = no ignore.
- When reviewing your own ignore list at session start, RUN each
  one first. The answer is usually "it works now."
- Prefer ``@pytest.mark.skipif`` inside the test (with a specific
  reason string) over a shell ignore — it documents the gate at
  the failure point and auto-resurrects when conditions change.

**Source:** 2026-04-23, end-of-session full-suite validation
revealed six false-blocker tests.

---

## 16. State-mutation routes need a CAS guard, not a pre-check + commit

**Cost:** 2026-04-26 adversarial-async hunt over a single session
turned up 12 production bugs — 9 of them the same anti-pattern in
9 different code paths. Without the hunt these would have been
discovered as data-corruption tickets in production.

**Cause:** Default ORM-style state mutation reads, decides,
writes:

    row = session.get(X, id)
    if row.status == EXPECTED:
        row.status = NEXT
        session.commit()
        recorder.record(SOMETHING)

The session.commit emits ``UPDATE x SET status=NEXT WHERE id=:id``
— no precondition. Two concurrent requests both pass the check,
both UPDATE, last writer wins. Each then records a side effect
(provenance event, audit row, terminal-state HTTP response),
giving you two terminal events for one transition.

**Confirmed instances in this session:** approve+reject (V1),
double-execute (V3), verified-synonym revoke last-writer-wins
(AA), sweeper double-record RUN_FAILED (Z), executor rebirth from
terminal (AB×3), verified-synonym scope=NULL pre-check race (Y),
plus orphan-PENDING composer fail (U) which is a non-CAS variant.

**Detection signal:** Any of:

- A route docstring claims "idempotency is the caller's job"
  about a check that's a Python ``if row.X != Y: raise 409``.
- A scheduled job (sweeper, queue drainer, reconciler) reads a
  row, decides, then mutates the same row.
- A SQLAlchemy ORM ``run.status = X`` followed by
  ``session.commit()`` where ``X`` is a terminal value.

**Mitigation:** Conditional UPDATE.

    result = session.execute(
        update(X)
        .where(X.id == id)
        .where(X.status.in_(LEGAL_PRECONDITIONS))
        .values(status=NEXT, ...)
    )
    session.commit()
    if result.rowcount == 0:
        raise 409   # or skip the side effect — somebody else owns
                    # this transition

OR: a partial unique index that turns the second concurrent write
into an IntegrityError (used for the sweeper's RUN_STARTED
single-claim, and for the verified-synonym scope=NULL case).

If the side effect is a ProvenanceEvent, the rowcount==0 branch
must NOT call recorder.record() — otherwise the loser still
emits a terminal event and the chain has two of them.

**Source:** 2026-04-26 adversarial async hunt, clusters U / V1 /
V3 / Y / Z / AA / AB. Same anti-pattern, same fix shape.

---

## 17. "ORDER BY id DESC" on a random-UUID PK is a bug, not an ordering

**Cost:** ~30 min to find + fix (cluster AC, 2026-04-26). User-
visible: roughly half the time, ``/hpc/confirm`` confirmed the
WRONG AllocationEstimate, silently — by pure UUID-lex coincidence.

**Cause:** ORM PKs in this schema are ``Mapped[UUID] = mapped_column(
primary_key=True, default=uuid4)``. uuid4 is random; lex-largest
has nothing to do with insertion order. Any code that does
``ORDER BY id DESC LIMIT 1`` to get "the latest" of something is
picking a row by chance, not by chronology.

**Confirmed instances:** ``/hpc/confirm`` AllocationEstimate
selection (cluster AC) — fixed by adding ``created_at`` column,
ordering by ``created_at DESC, id DESC``. Cluster X
(ProvenanceEvent ``ORDER BY timestamp DESC, id DESC``) had a
related variant: real timestamp present, but ties at microsecond
resolution still resolved by random-UUID tiebreak — fixed by an
in-memory cursor cache in ProvenanceRecorder.

**Detection signal:** Any of:

- ``ORDER BY <PK> DESC`` where the PK is a UUID (random) and
  there's no other ordering column on the table.
- ``ORDER BY <ts> DESC, <PK> DESC`` where ``<ts>`` has microsecond
  resolution and concurrent appends are common — the tiebreak
  effectively picks a random row when timestamps tie.
- "Latest" semantics in a route docstring or comment, paired with
  a SELECT that doesn't have a monotonic ordering key.

**Mitigation:** Add a ``created_at: Mapped[datetime]`` column at
the model level, set it on every INSERT, ORDER BY it. For
existing rows, backfill with the migration timestamp; tiebreak
on PK only for the unlikely tied-microsecond case.

**Companion mitigation when** the table is ProvenanceEvent or
similar append-only-but-validated chain: maintain a per-process
in-memory "last hash per run_id" cache, updated AFTER successful
commit, so the writer doesn't depend on the DB query's tiebreak
behavior. NOTE: that cache is *per-instance* — see #18.

**Source:** 2026-04-26 cluster X (recorder cache) + cluster AC
(allocation_estimate.created_at).

---

## 18. Per-instance caches break when more than one instance writes

**Cost:** ~25 min to find + fix (cluster AD, 2026-04-26).
User-invisible until the chain is validated; once ``recorder.
validate(run_id)`` ran, ``ChainBroken`` was thrown.

**Cause:** ``ProvenanceRecorder._last_hash: dict[UUID, str]`` —
the cluster X cache that fixes tied-timestamp UUID-tiebreak forks
— is per-instance. The Control Plane was creating two recorders
per process (one for HTTP routes, one for composer + executor).
Both write to the same run_id. Each maintains its own private
cache. When recorder A writes, then recorder B writes, then
recorder A writes again, A's cache still says "my last write was
the OLD event"; A picks that as prev, missing B's intervening
events. Chain forks.

**Detection signal:** Any of:

- More than one instance of a class that maintains a per-key
  in-memory cache for a shared resource.
- A class docstring promises "concurrency model: single process,
  multiple OS threads" but the production code constructs the
  class twice.
- Hash-chain validation passes in single-recorder unit tests but
  fails when two distinct write paths exercise the same run.

**Mitigation:** Single instance per process, plumbed end-to-end.
``create_app(recorder=...)`` and
``_build_components_from_env(engine, recorder=...)`` both accept
a recorder so the serve path builds one and shares it. A
structural test (``executor._recorder is shared``) catches
refactors that re-introduce the second instance before any chain
even forks.

**Source:** 2026-04-26 cluster AD.

---

## 19. Conditional UPDATE that "skips silently" + caller that fabricates the result

**Cost:** ~25 min to find + fix (cluster AJ, 2026-04-26). User
pushback was the trigger — "Make sure your code paths do not
cause silent failures that would make tests pass but would
impede the actual product use. Follow fail fast policy."

**Cause:** Friction log #16 prescribed conditional UPDATEs +
``rowcount == 0`` → log + skip the side effect. That is correct
on the WRITER side. The trap: the CALLER often computes a
result/response from ``what I intended to do`` rather than
``what's actually true``. The helper logs a warning, returns
to the caller, and the caller still constructs
``ExecutionResult(status=COMPLETED, ...)`` even though the
helper's UPDATE was rejected and the run is actually FAILED.

End result is exactly the silent-failure shape:
- the test of the helper alone passes (transition is correctly
  skipped, no double terminal event),
- the test of the API response with a swept run might not
  exist (was missing in this codebase),
- production sees an executor that reports COMPLETED for a
  workflow it didn't drive,
- the user / MCP tool / status poll sees "completed" and moves
  on without noticing the run is actually FAILED in the DB.

**Detection signal:** Any of:

- A function calls a helper that can return without performing
  its named action, and constructs a hand-written success
  response immediately afterward.
- The helper has a ``log.warning(... skipping ...)`` line and
  no return value (or a return value the caller ignores).
- A test asserts the helper's behavior in isolation but no
  test asserts the surrounding function's behavior when the
  helper skipped.
- "Conditional UPDATE WHERE preconditions" is paired with a
  caller that ignores ``rowcount``.

**Mitigation:**
- Helpers that perform a state transition return a ``bool`` (or
  raise) indicating whether they actually performed it.
- Callers route every terminal result through a single helper
  (e.g., ``_terminal_result``) that consults the actual DB
  state when the writer was rejected. NEVER hand-construct a
  terminal status in multiple places.
- Add an integration test that exercises the API surface with
  the precondition-violating state pre-installed (e.g.,
  pre-flip the run to FAILED, then call ``execute()``; assert
  the response status is FAILED, not COMPLETED).

**Source:** 2026-04-26 cluster AJ. Same pattern bit twice in
follow-ups: the IntegrityError handler returned a fabricated
``status=RUNNING``, and the run-not-found early-return
fabricated ``status=FAILED``. Both routed through
``_terminal_result`` after the audit.

---

## 20. Pydantic BaseModel default-allow-extras silently swallows YAML typos

**Cost:** ~10 min to find + audit + fix (probe batch 36, 2026-04-27,
proactive find via adversarial probe 955).

**Cause:** Pydantic's default ``Config.extra`` is ``'allow'``. A
``BaseModel`` subclass with no explicit ``model_config`` silently
accepts arbitrary unknown keys. In a YAML-driven config
(``synthesis_config.yml``, ``composer_config.yml``), an operator's
typo (``max_rag_chuncks: 8`` vs. ``max_rag_chunks: 8``) is silently
ignored — the typoed key disappears, and the schema's *default*
value is used. They might bump the typoed value to 16 thinking
they raised the cap; the synthesizer continues to use the
original 8.

Exactly the silent-failure shape the user-directive forbids:
"Make sure your code paths do not cause silent failures that
would make tests pass but would impede the actual product use.
Follow fail fast policy."

**Detection signal:** Any of:

- ``grep -rn 'class .*(BaseModel)' --include='*.py' src/`` —
  for each match, check the next ~5 lines for
  ``model_config = ConfigDict(extra=``; if absent, this shape.
- A YAML config carries a key the schema accepts at-parse but
  silently uses defaults at runtime.
- An operator reports "I changed X in the YAML and nothing
  happened."

**Mitigation:**

- Workspace rule (added 2026-04-27): every ``BaseModel`` subclass
  in ``apecx_integration/`` MUST set
  ``model_config = ConfigDict(extra='forbid')`` unless the class
  explicitly needs open shape (rare; document the exception).
- Audit pass after the rule landed: ``SynthesisConfig`` and
  ``ComposerConfig`` fixed; ``_APIBase``, ``_EntityBase`` already
  compliant. No further offenders in apecx_integration/ as of
  2026-04-27.
- For a new ``BaseModel``, write the ``model_config`` line in
  the same commit — defer-it = forget-it.

**Source:** 2026-04-27 batch 36 probe 955 (synthesizer.py).
Audit pass also fixed composer_schemas.py.

---

## How to add to this log

- Only entries that ate ≥3 min or recurred across turns.
- Each entry needs: cost, cause, mitigation, detection signal.
- Delete entries when the mitigation is durable and the detection
  signal stops firing for a week.
- If you're debating whether to add an entry, the friction was
  probably smaller than it felt. Don't add it.
