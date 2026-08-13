# BV-BRC / Globus query-injection sweep (2026-08-12)

After fixing the parenthetical-synonym 400 in `BvbrcTaxonomySearchStep` (commit `aa0754c`),
two parallel read-only sweeps checked whether the same bug class — interpolating an
LLM/synonym/name value into a query DSL without escaping the DSL's metacharacters — recurs on
the system's two external-query surfaces.

## Globus / Lucene surface — ALREADY SAFE

`agents/globus_search/structural_query.py` routes every value interpolated into a Globus
*advanced* (Lucene) query through `_quote_advanced_phrase` (`:37-49`), which double-quotes the
term and escapes `\` and `"`. Inside a quoted phrase, parens/colons/slashes/brackets/AND/OR/NOT
are all literal, so the reserved-char recall hole cannot occur. Its docstring already names the
exact failure mode (`A/Puerto Rico/8/1934(H1N1)` → silent zero results) — a deliberate,
already-shipped fix for this class. The harmonized-search leg puts NAMES into structured
`match_any` filters (not Lucene-parsed) and the raw leg runs simple mode (reserved chars are not
operators). No action needed.

## BV-BRC / RQL surface — ONE additional vulnerable site

`composition/steps/bvbrc_protein_fasta_step.py:497` builds `eq(product,"{protein}")` (exact) and
`eq(product,*{protein}*)` (wildcard). `protein` is a NAME (raw user/LLM term, or a catalog
product from `ProteinNameNormalizationStep`), only `.strip()`'d — no paren/colon sanitization.
Same class as the taxonomy bug, but **worse failure mode**: a 400 here RAISES
(`_get_json` → `raise_for_status` → uncaught in `_fetch`/`process`) and fails the whole
conservation/FASTA leg, vs the taxonomy step's per-synonym warn-and-skip.

All other RQL interpolation sites are numeric-id or fixed-vocabulary (safe): the sweep checked
`bvbrc_protein_fasta_step.py:474/512`, `protein_name_normalization_step.py:263`, `_bvbrc_cds.py:28`
— all `eq(taxon_id,<int>)` / `in(feature_type,<const>)`. `sequence_analysis_step.py:188` builds a
Solr `select` on a name but is RETIRED (raises before the query is built) — not exploitable.

## Confirmed remedy (live probe, 2026-08-12)

- Real BV-BRC CDS products in a 50-row `*glycoprotein*` sample contain **no** parentheses.
- `eq(product,"spike glycoprotein (S)")` → **400**; wildcard → 400; backslash-escaped parens →
  400 (no RQL escape works); paren-stripped `"spike glycoprotein"` → **200, 1 row**.
- Conclusion: a paren-bearing `protein` value is always an LLM/user gloss (never a real product),
  and stripping recovers the matching form — so the **same strip remedy applies**, and the
  `_rql_safe_name` helper is directly reusable.

## Next task (its own /feature cycle — NOT done here)

Fix `bvbrc_protein_fasta_step.py:497`:
1. DRY: extract `_rql_safe_name` from `bvbrc_taxonomy_search_step.py` to a shared module
   (e.g. `composition/steps/_bvbrc_rql.py`); import it in both steps (updates the already-
   committed taxonomy step — cohesive on this branch).
2. Apply `_rql_safe_name` to `protein` before building the exact + wildcard `eq(product,...)`
   clauses; keep the existing empty-skip semantics.
3. Tests: the shared unit test already pins `_rql_safe_name`; add a network-gated integration
   test that a paren-bearing protein term resolves (does NOT raise) against real BV-BRC.
4. review-gate + `Reviewed:` trailer + index regen, as with the taxonomy fix.

Deferred (unchanged): the colon-case RQL break (constructed, not observed from real LLM output).
