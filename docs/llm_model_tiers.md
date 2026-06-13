# LLM model tiers — default vs quality (E3-14)

The synthesis path resolves its model through a single source of truth,
`apecx_integration.agents._llm_config.resolve_llm_model()` (E3-6): `APECX_LLM_MODEL`
env var > the default `nemotron-3-nano:4b`. `apecx-setup` pulls exactly that model,
and a startup preflight fails loud with `ollama pull <model>` if it is absent.

## The two tiers

| Tier | Model | When |
|---|---|---|
| **Default (fast)** | `nemotron-3-nano:4b` | Out-of-box. ~700 MB, low latency. The 5-section output contract is **structurally guaranteed regardless of model** (`_ensure_contract_headers` injects any LLM-omitted heading; Sources/Follow-ups/Structural are deterministic), so the document is always well-formed. |
| **Quality** | `mistral-nemo:latest` | Set `APECX_LLM_MODEL=mistral-nemo:latest` (then `apecx-setup llm` pulls it). ~7 GB, ~12B params. |

```bash
# quality tier:
export APECX_LLM_MODEL=mistral-nemo:latest
apecx-setup llm        # pulls it; the resolver + preflight now target it
```

## Brutal-truth tradeoff (why this is a real tier, not a cosmetic knob)

On a 4B model the **contract holds but the reasoning is shallow**: the narrative
sections (`# Answer`, `## Cross-data reasoning`, `## Integrated insight`) are
thinner, and the citation gate is hit more often (the 4B model more frequently
emits malformed/hallucinated citation tokens → the **degrade-loud** path fires,
which preserves all retrieved evidence in the deterministic Sources section but
withholds the LLM narrative). This was observed repeatedly during v2.1 e2e runs
(fullwidth-bracket citations, 2-token hallucinations). The *evidence and the
science stages* (sequence conservation, structural SASA, functional cross-check)
are **LLM-independent and identical on both tiers** — only the synthesized prose
quality differs. So a 4B run never ships wrong science; it ships a shallower
narrative (or a withheld-narrative degrade with the evidence intact).

`mistral-nemo` produces deeper cross-data reasoning and passes the citation gate
more reliably. It is the documented **composer** baseline already (the composer is
a separate tier with per-role models — see `composer_config.yml`); aligning the
synthesis default to it is a deliberate operator choice, not the out-of-box
default, because the 4B model keeps a fresh install fast and the contract still
holds.

## What is NOT measured here (honest scope)

A rigorous per-stage quality delta (run the same query on both models, score the
narrative) would cost two full ~6–10 min e2e runs and a scoring rubric; it was not
run. The qualitative difference (depth + citation-gate pass-rate) is recorded from
the v2.1 e2e observations above. An operator who needs the measured delta for a
specific deployment can run the evidence e2e under each model and compare the
`## Cross-data reasoning` / `## Integrated insight` sections and the
`status`/degrade rate.
