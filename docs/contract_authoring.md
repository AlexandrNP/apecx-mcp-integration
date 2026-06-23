# Authoring data-unit I/O contracts (Project A)

Workflow data units can declare an optional **contract** describing what they carry. When a
DirectLink's two endpoints BOTH declare a contract, the framework checks them for compatibility
at load (and, under `config_version: 3`, enforces the actual value at runtime). Contracts are
**gradual**: an undeclared side is `any` and always compatible, so existing untyped workflows are
unaffected. This is how a long-lived, multi-contributor workflow library catches interface drift
instead of silently consuming it.

## Where a contract attaches

In a step wrapper YAML, on a data unit under `input_data_units` / `output_data_units`:

```yaml
output_data_units:
  synthesis_bundle_output:
    class: "nanobrain.core.data_unit.DataUnitMemory"
    name: synthesis_bundle_output
    contract:
      kind: record
      required:
        query: {kind: text}
        rag_chunks: {kind: collection}
```

## The kind lattice

| kind | the value is | refinement (optional) |
|---|---|---|
| `text` | a string | — |
| `file` | a file path | `extensions: [fasta, fa]` (accepted extensions) |
| `record` | a dict | `required: {key: <nested contract>}` and/or `required_keys: [k1, k2]` (kinds = any) |
| `collection` | a list/tuple | `element: <nested contract>` |
| `handle` | an opaque reference | `referent: <nested contract>` |

`record` is **open-world**: extra keys are fine; only the `required` keys are checked. An
undeclared value-kind on a required key is `any` (not checked) — declare incrementally.

## How to annotate a boundary (the rule that matters)

A DirectLink boundary is **covered** only when BOTH endpoints declare a contract — the producer's
`output_data_units[...]` and the consumer's `input_data_units[...]`. Annotate **both**, and make
them compatible (the consumer's `required` keys must be a subset of what the producer guarantees;
matching kinds). **Verify against the step's real I/O** — read the step's `process()` / the data
unit's documented shape; a WRONG contract is worse than none (false warning or false confidence).

## The ratchet (don't fight it)

`tests/integration/test_contract_ratchet.py` counts boundaries lacking both-endpoint coverage and
pins it to a `BASELINE`. The count can only go DOWN: a PR that adds an unannotated boundary fails.
When you annotate a boundary, the count drops — **lower `BASELINE` to the new live count** (the
test tells you the number). Never raise the baseline to make a red test pass; annotate instead.

Scope (Step 3a): the ratchet currently counts **DirectLink** boundaries only (the corpus is
overwhelmingly DirectLink). The runtime checker runs on every link class, so a ConditionalLink
boundary is enforced at load/runtime but not yet counted here — widen `contract_coverage.py` if
ConditionalLink boundaries become common.

## Enforcement is opt-in

At `config_version: 1`/`2` (the default) a mismatch only WARNs at load (non-binding). A workflow
opts into BINDING enforcement — load-time RAISE on an incompatible declared boundary, runtime RAISE
on a value that violates its declared output contract — by setting `config_version: 3`. The
contract algebra + the runtime guard live in `nanobrain/core/data_contract.py`.
