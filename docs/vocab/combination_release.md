# Vocabulary — `CombinationReleaseStep`

Developer reference for the user-facing markdown + output-bundle keys emitted by
`composition/steps/combination_release_step.py`. Not loaded at runtime — keep the inline
strings in sync. Harmonized with `peptide_candidate_assessment_step` (the closest sibling
that also renders an approval-gated assessment). Register: domain nouns, cautious claims —
no build/assemble/construct verbs, no validation claims.

Markdown section headers (mirror `peptide_candidate_assessment_step`):

| state | headers |
|---|---|
| withheld (no/invalid approval) | `## Evidence readiness`, `## Approval requirement`, `## Validation gaps` |
| approved | `## Summary`, `## Epitopes`, `## Evidence`, `## Validation gaps`, `## Sources and evidence`, `## Limitations` |

Output-bundle `parts` keys:

| key | meaning |
|---|---|
| `combination_released` | bool — was the detailed combination output released (was `group_assessment_released`) |
| `epitopes` | per-epitope records (sequences only when released) |
| `epitope_support` / `structural_placement` / `combination_support` / `immunodominance` | the four classifications (see combination_classification.md) |
| `validation_gaps` | what this workflow did NOT establish |
| `approval` | `{required, token, scope_query, protein}` |

Cautious-register phrases that MUST remain (the dual-use posture lives here, not in a
word-ban — `_assert_neutral_output` was removed):
- "does not establish combination-level behavior or readiness for use"
- "No combined sequence, epitope order, linker, or construct-design instructions are produced."
- "not an external validation, construct-design plan, or operational determination"

`_safe_reason` is RETIRED: the withheld output now states the approval `reason` verbatim
(matching `peptide_candidate_assessment_step._withheld_output`). "connector"→"linker",
"component order"→"epitope order", "implementation instructions"→"construct-design
instructions".
