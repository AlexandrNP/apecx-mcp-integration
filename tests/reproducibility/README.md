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
3. Write `prompt.txt` — the full composer prompt.
4. Run the composer once (pinned LLM version, temperature=0) and
   capture the output bytes. Write them to `baseline_content.<ext>`.
5. `sha256(output) | hex | head -c 64 > baseline_hash.txt`. Or in
   Python: `hashlib.sha256(output).hexdigest()`.
6. Commit all three / four files together; the fixture is active
   immediately and gets exercised by the reproducibility test suite.

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

## Seed fixture

`violin_bvbrc_synonym_gate_minimal/` ships as the T12 seed. Its
`baseline_hash.txt` is a placeholder until the composer lands and
the real baseline can be captured. The `test_baselines.py` suite
auto-skips when the composer is not importable, so the placeholder
hash does not block the unit suite.
