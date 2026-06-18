# Vocabulary — `CombinationClassificationStep`

Developer reference for the classification labels emitted by
`composition/steps/combination_classification_step.py`. Not loaded at runtime — keep the
inline strings in sync. Harmonized with `viral_epitope_analysis` /
`conserved_epitope_candidate_assessment`. Register: domain nouns, cautious claims.

The step emits four classifications under `preliminary`:

| key | dimension | label vocabulary |
|---|---|---|
| `epitope_support` | per-epitope evidence strength | `approved candidate peptide` / `direct epitope-level support` / `reported epitope-level support` / `location-only support` / `source-described support` / `insufficient evidence` |
| `structural_placement` | where epitopes sit on a common structural reference | `common-reference support` / `coordinate-only support` / `partial coordinate support` / `insufficient placement basis` |
| `combination_support` | evidence that the epitopes behave together | `direct combination-level support` / `epitope-level support only` / `insufficient evidence` |
| `immunodominance` | relative dominance between epitopes | `direct immunodominance records` / `epitope-level immunodominance metadata` / `not evaluated` |

Key renames (vs. retired euphemisms): `component_support`→`epitope_support`,
`placement_support`→`structural_placement`, `group_support`→`combination_support`,
`balance_considerations`→`immunodominance`. "group-level"→"combination-level",
"item-level"→"epitope-level", "balance"→"immunodominance".

Evidence-bundle input keys read: `combination_evidence` (or `multi_epitope_constructs`),
`immunodominance_evidence` (or legacy `dominance_evidence`). These are caller-supplied and
optional; absence degrades to `insufficient evidence` / `not evaluated`, never an error.
