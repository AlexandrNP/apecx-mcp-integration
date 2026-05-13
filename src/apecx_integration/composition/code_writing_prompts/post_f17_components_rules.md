# Post-F17 component rules

Imperatives for composing workflows with the three new components
shipped after F17: `MultiSampleDrafterStep`, `ConsensusAggregatorStep`,
`SolutionMemoryStep`. Pairs with `nanobrain_rules.md` (general framework
rules) and `example_step.md` (BaseStep authoring pattern).

## Decision matrix — which drafter / which voter / which memory

| Goal | Drafter choice | Voter choice | Memory choice |
|---|---|---|---|
| Maximum determinism (default) | `BenchmarkDrafterStep` (T=0.0) | none (single sample) | omit |
| Cover hard problems via sample variance | `MultiSampleDrafterStep` (N=3, T=0.5) | `ConsensusAggregatorStep` | omit |
| Reuse passing patterns across runs | any drafter | optional | `SolutionMemoryStep` read+record |
| Adoption-pitch demo of all three | `MultiSampleDrafterStep` | `ConsensusAggregatorStep` | `SolutionMemoryStep` read+record |

Pick the LEFTMOST viable column. Multi-sample is more expensive
(linear in N) and has been measured to regress on this model
(F18). Memory only pays off across many problems in the same
category.

## MultiSampleDrafterStep — REQUIREMENTS

1. NEVER set `temperature: 0.0` with `n_samples > 1`. The config
   validator FAIL-FASTs (identical samples are silent-failure noise).
2. ALWAYS pair with a downstream voter (`ConsensusAggregatorStep`).
   A bare multi-drafter without a voter wastes the fan-out.
3. SET `n_samples: 3` and `temperature: 0.5` as the default starting
   point on local 12B models. Raise N only after a benchmark sweep
   shows lift.
4. SET `request_timeout_seconds` high enough for the slowest sample,
   not the average — parallel calls are bounded by the slowest.

## ConsensusAggregatorStep — REQUIREMENTS

1. CHOOSE `voting_strategy: ast_validator` (default) for nanobrain-
   framework code. `runtime_validator` for deeper checks (slower,
   subprocess). `first_non_empty` for MBPP-style algorithmic code
   where no validator is meaningful.
2. ALWAYS link from a multi-sample upstream OR a single-shot drafter
   (the aggregator wraps single `code_source` as a 1-element list).
3. DO NOT discard the aggregator's `voted_passes` field downstream —
   `SolutionMemoryStep` with `record_only_if_pass: true` depends on
   it as the gate signal.

## SolutionMemoryStep — REQUIREMENTS

1. USE `mode: read` UPSTREAM of the drafter (enrich the spec with
   cached examples). USE `mode: record` DOWNSTREAM of the aggregator
   (persist the winning candidate).
2. SET `record_only_if_pass: true` for record-mode whenever an
   aggregator is upstream (avoids accumulating noise in the bucket).
3. SET `store_path` to a deployment-scoped absolute path. The default
   (`composition/_runtime/solution_memory.json`) is shared and may be
   undesirable across deployments.
4. NEVER raise on cache miss. The component is silent-failure-tolerant
   BY DESIGN — a missing cache must not break the codegen path.

## Workflow-wiring REQUIREMENTS for these components

1. EVERY DirectLink MUST declare `auto_transfer: true`. Without it,
   the link silently no-ops on every trigger (G7 default-flip is
   pending in `config_version: 1`; new workflows declare
   `config_version: 2` AND `auto_transfer: true` explicitly).
2. WHEN `memory_recorder` is in the chain, declare a SECOND
   workflow-level output port (e.g., `workflow_recorder_status`) and
   link the recorder's output to it. The framework's data-flow
   integrity validator FAIL-FASTs on orphaned step outputs.
3. PRESERVE `task_category` in EVERY step's output between the
   router and the recorder. A silent drop defeats per-category
   memory bucketing.

## Topology templates (use as starting points)

### Template A — F17 winner (BEST on nanobrain-native, 80%)

    workflow_input -> task_router -> drafter -> workflow_output

(single drafter, T=0.0, no memory, no consensus). Use this when in
doubt.

### Template B — structural consensus (F18: -10pp vs A on this model)

    workflow_input -> task_router -> multi_drafter (N=3, T=0.5)
        -> aggregator (ast_validator) -> workflow_output

### Template C — memory-augmented (predicted-best for cross-run reuse)

    workflow_input -> task_router -> memory_reader (read mode)
        -> drafter -> aggregator (single-candidate path)
        -> memory_recorder (record_only_if_pass) -> workflow_output

### Template D — all three combined (`benchmark_integrated_full`)

    workflow_input -> task_router -> memory_reader (read)
        -> multi_drafter (N=3, T=0.5) -> aggregator
        -> memory_recorder (record_only_if_pass) -> workflow_output

Side-effect: `memory_recorder.memory_recorder_output` flows to a
SECOND workflow port `workflow_recorder_status` (framework's
integrity validator requirement).

## Authoring path — two legitimate forms

1. **YAML** (canonical, version-controlled):
   `benchmark_*/workflow.yml` + `benchmark_*/steps/*.yml`
2. **WorkflowBuilder** (programmatic, code-generation-friendly):
   `benchmark_structural_consensus_lightweight_builder.py`. NOTE
   the framework's `WorkflowBuilder.add_link` emits FLAT link
   entries that the loader silently drops; apply the
   `_nest_link_configs` helper from that file BEFORE calling
   `builder.load()`.

Both forms MUST produce identical step ids and link counts; pair
them with a parity test (see
`tests/integration/test_structural_consensus_lightweight_parity.py`).

## Pin: forbidden authoring patterns

- DO NOT override `execute()` in any step class. Implement `process()`.
- DO NOT define a step's data units at workflow level. Step OWNS them.
- DO NOT use inline-dict `config:` for a Step subclass under the
  WorkflowBuilder. Path-reference only (framework's CLOSED-CLASS
  rule).
- DO NOT use `ConditionalLink → workflow_output`. Silent-no-op
  failure mode (F11). Use DirectLink + a conditional reviser overwrite.
- DO NOT hardcode a system prompt in Python. Use `system_prompt_file:`
  on the step config.
- DO NOT skip `_strip_framework_keys` in a step's `Config` class. The
  framework's class-routing prepends a `class:` key that Pydantic
  `extra='forbid'` would reject without this mode='before' validator.
