# Skeleton Catalog

Pre-authored `MinimalWorkflowSpec` instances under
`composition/skeletons/`. Each file is one skeleton; the composer
loads them at init and surfaces them in the spec-mode prompt so the
LLM can pick by name.

## Shipped skeletons (2026-05-12)

| Name | Steps | Best for |
|---|---|---|
| `synthesis_pipeline` | `SynthesisContextAssemblyStep` → `RagSynthesisStep` | Cross-corpus synthesis answers ("explain X", "what does the literature say about Y"). |
| `entity_extraction_only` | `EntityExtractionStep` | Single-step NER ("extract entities from this text"). |
| `pathogen_bvbrc_match` | `EntityExtractionStep` → `EnhancedBVBRCMatchStep` | Pathogen names → BV-BRC genome ids. |
| `pubmed_only_literature_search` | `PubMedHarvesterStep` | Raw citations + abstracts, no synthesis. |
| `rag_domain_search_only` | `DomainRagSearchStep` | Top-k semantic chunks, no LLM. |
| `violin_bvbrc_context_only` | `VIOLINBVBRCContextStep` | Pure pandas lookup against VIOLIN/BV-BRC. |

## Web-research-informed patterns the catalog does NOT yet ship (deferred)

Each of these is a real workflow pattern surveyed in 2026 RAG /
agentic-AI literature. They require step classes that do NOT exist
in the current apecx component catalog; authoring a skeleton that
references invented classes would be the exact hallucination shape
CPR exists to prevent. Filing here so a future operator can pair
each pattern with a real step authoring task.

### Reflection / self-critique pattern

Cycle: `Generate → Reflect → Refine`. The LLM produces an initial
answer; a critic (another LLM call or a tool) evaluates against
criteria; the answer is revised. See
arxiv.org/abs/2501.09136 (Agentic RAG Survey, 2026) §3.2.

**To ship**: needs a new `WorkflowOutputReviewStep` (semantic-fit
review of the workflow's output, not the workflow itself). The
APECx `WorkflowReviewer` (REVIEW-AGENT, 2026-05-12) is the
composer-level analog; a workflow-level reviewer would generalize
it.

### Multi-hop retrieval

Cycle: `Retrieve₁ → decide-if-more-needed → Retrieve₂ → Synthesize`.
The first retrieval informs the second query. See
arxiv.org/abs/2506.00054 (RAG comprehensive survey, 2026) §4.1.

**To ship**: needs a `QueryRefinementStep` that takes the first-pass
retrieval output + the original prompt and produces a refined query.

### Self-consistency / N-best voting

Generate `N` independent answers, vote / aggregate. See the
deeplearning.ai post on agentic design patterns.

**To ship**: needs a `MultiAnswerAggregationStep` that takes a
list of candidate answers and picks / merges. Could also be done
at the framework level via parallel `RagSynthesisStep` instances
fan-in via a custom aggregator.

### Code-writing flow

Cycle: `RequirementsParseStep → TestGenerationStep → CodeWriteStep →
TestRunStep → Refine`. Different audience from the current apecx
biological-corpus stack; would require a dedicated coding-agent
component family.

**To ship**: out of scope for the apecx biological-research stack
in this version. Document as a non-goal unless the product scope
expands to code generation.

## Authoring guidance for future skeletons

1. Compose only EXISTING components — read the wrapper YAML to get
   exact `input_data_units` / `output_data_units` names.
2. Test with a `_StubLLM` returning `{"skeleton": "<your_name>"}`
   and assert the expanded workflow's YAML contains the canonical
   class paths.
3. Add a row to the table above with the link topology.
4. Cite the inspiration (paper / project) when adapting an external
   pattern — helps the next operator decide whether to update or
   delete the skeleton when the source pattern evolves.
