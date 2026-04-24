# Reproducibility fixtures (T12)

Each subdirectory is one fixture: a frozen (prompt, expected-output)
pair the composer is re-run against to detect silent drift.

## Why this exists

The composer (T06 / Phase 2) generates workflow YAML and novel
Python from a scientist's description. Same input + same pinned LLM
version + same library version should produce the same output. If
that contract breaks without anyone noticing, the "regenerate this
workflow from last month's prompt" UX quietly produces different
results — and the artifact versioning story (T11) becomes
meaningless.

## Fixture layout

```
fixtures/
  <fixture_name>/
    prompt.txt              # full composer prompt
    kind                    # "yaml" | "python"
    baseline_hash.txt       # sha256(generated_bytes) as hex, 64 chars
    baseline_content.yml    # optional; the bytes the hash was taken over
    baseline_content.py     # (use either .yml or .py depending on kind)
```

`baseline_content.*` is **optional but recommended**. When the hash
check fails, the harness falls back to semantic-equivalence
comparison (YAML dict equality / Python AST equality) against this
file. Without it, any hash mismatch is a hard fail even when the
content is functionally identical.

## Adding a fixture

1. Pick a name. Use `<domain>_<scenario>[_<variant>]`, e.g.,
   `violin_bvbrc_synonym_gate_minimal`.
2. Create the directory under `fixtures/`.
3. Write `prompt.txt` — the full composer prompt, and `kind`
   (currently always `yaml`; the harness only tests the YAML byte
   path today).
4. Two capture modes:

   **a. Placeholder-LLM fixture (deterministic, runs in CI).**
   Write `canned_response.txt` containing a ```yaml fenced block (and
   optionally a ```novel_python fenced block). Then run:

   ```bash
   .venv/bin/python scripts/capture_fixture_baselines.py --only <fixture_name>
   ```

   The script extracts the YAML body with the composer's own fence
   regex, writes `baseline_content.yml`, and writes `baseline_hash.txt`
   to match. This guarantees byte-parity with what the composer's
   `_parse_response` would extract from a real LLM response with the
   same content.

   **b. Live-LLM fixture (operator-captured, CI-skipped).**
   Skip `canned_response.txt`. Run the composer once against the
   pinned model at temperature=0, capture the output bytes to
   `baseline_content.yml`, and write `sha256(output)` to
   `baseline_hash.txt`. The harness auto-skips live-LLM fixtures
   unless `APECX_T12_RUN_LIVE_LLM=1`.

5. Commit the fixture files together. The fixture is active
   immediately and gets exercised by the reproducibility test suite.

## What each fixture should exercise (diversity targets)

Ten fixtures covers roughly this pipeline-path matrix — add new
fixtures only when they extend one of these axes:

- **Step count:** 1, 2, 3, 4 (longer chains stress link resolution).
- **Link count:** 0 (empty `links: {}`), 1, 2, 3.
- **Novel Python:** absent / one step / multiple steps (exercises
  the T13 import-scanner branch — a scan violation raises before
  the hash step, so fixtures with novel Python implicitly pin
  "this code still passes the whitelist").
- **Library-component reuse:** different library classes in different
  combinations so that a class-path rename in one place shows up
  loudly in exactly one fixture rather than quietly in all of them.

## Updating a fixture (legitimately)

When you deliberately change the composer, the library version, or
the pinned LLM, many fixtures will drift. That's expected. Re-capture
each one with the new inputs and commit the new baseline. The commit
message must explain **why** the output changed — otherwise the next
reviewer can't tell re-capture-for-good-reason from re-capture-to-
silence-a-test (the exact thing this suite exists to prevent).

## Temperature = 0

All composer calls for fixture capture use temperature=0. LLMs are
not fully deterministic even at t=0 (probability ties flip), which
is why the semantic-equivalence fallback exists. Record the LLM
model version in the fixture commit message.

## Current fixture inventory (2026-04-24)

Ten fixtures: three virus-domain (`bvbrc_query_only_minimal`,
`entity_extract_then_rank`, `violin_bvbrc_synonym_gate_minimal`) and
seven generic non-virus (`single_step_empty_links_generic`,
`two_step_reader_to_ranker`, `three_step_file_approval_writeback`,
`four_step_reader_cache_writeback_rank`, `single_novel_python_step`,
`library_plus_novel_python_filter`, `two_novel_python_steps_chained`).

All ten are placeholder-LLM fixtures that run in CI. Live-LLM
fixtures (captured against a real pinned model) are the remaining
T12 AC1 work — they require operator time and model pinning, not
authoring.
