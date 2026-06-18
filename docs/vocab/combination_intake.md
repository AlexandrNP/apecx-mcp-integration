# Vocabulary — `CombinationIntakeStep`

Developer reference for the display + identifier terms used by
`composition/steps/combination_intake_step.py`. Not loaded at runtime — keep the inline
strings in the step in sync with this glossary. Terms are harmonized with the sibling
workflows `viral_epitope_analysis` and `conserved_epitope_candidate_assessment`; where a
sibling already owns a term, that is the source of truth. Register: domain nouns, but
cautious verbs/claims (no build/assemble/construct).

| term used here | meaning | source of truth |
|---|---|---|
| epitope | a submitted antigenic segment (the unit being combined) | `viral_epitope_analysis` |
| additional epitopes | caller-supplied epitopes beyond the candidate (public input `additional_epitopes`) | this workflow |
| candidate peptide | the approved item carried over from the prior assessment (`role: candidate`) | `peptide_candidate_assessment_step` ("minimal consensus peptide candidate") |
| candidate-peptide assessment | the upstream `conserved_epitope_candidate_assessment` run | sibling workflow name |
| epitope combination | the candidate peptide + additional epitopes considered together | this workflow |
| `combination_request` | the public input envelope key (was `combination_input`) | this workflow |
| scope / `scope_query` | the approval-binding fingerprint (per-epitope sha256 + coords) | shared with `peptide_candidate_assessment_step` |

Notes:
- A miss output uses the sibling section headers `## Evidence readiness` and
  `## Approval requirement` (matching `peptide_candidate_assessment_step._needs_evidence`).
- "component", "grouping", and "prior item" are RETIRED euphemisms — do not reintroduce.
