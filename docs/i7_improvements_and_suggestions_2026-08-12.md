# I7 — improvements & suggestions (2026-08-12)

Surfaced by the live-data verification run + two parallel cross-checks (code-vs-plan,
manuscript-vs-shipped). Ordered by consequence. Each item is tagged **[decision needed]**
(author must choose) or **[actionable]** (safe to implement without judgment calls).

---

## 1. [decision needed] The shipped I7 feature contradicts the manuscript's central claim

**The conflict.** The main text asserts resolution is *fully deterministic* and that the
model "never decides how an entity resolves or which records match... it cannot hallucinate
a match" (`latex/apecx_patterns.tex:271-276`; reinforced by Table 4 rows at `:573-574`
labelling organism/gene resolution "Deterministic"). But I7 places a local model in
candidate *selection* on the resolution path (`TaxonCandidateReviewStep` performs a second
LLM call that selects the taxon id — confirmed in the live run log). The supplement's
fallback ladder (`S3_methodology_ontologies.md` S3.6) terminates at dictionary → raw-text
fallback with **no LLM tier**, and the S7 roadmap (`S7_improvements.md`) has I1–I6 but **no
I7 row**.

**Why it can't be half-fixed.** Adding I7 to the supplement while leaving the main-text
"fully deterministic" claim intact would create an internal paper/supplement contradiction —
worse than the current code/paper gap. Both halves move together or neither does.

**Recommended reconciliation (preserves the thesis, needs author sign-off).** The paper's
core honesty claim can survive with a *qualifier*, not a reversal, because acceptance remains
deterministic even though proposal/ranking is not:

> As a bounded last resort — reached only after dictionary and harmonized-search both miss —
> a local model proposes and ranks candidate taxa. Each candidate is accepted only if it
> passes an upstream catalog verification (a non-zero coding-sequence count under that taxon);
> an un-verifiable proposal is rejected, not served. The model widens the *proposal* set under
> starvation; the *acceptance decision* stays a deterministic, upstream-verified function that
> cannot hallucinate a match.

Under this framing the main-text claim at `:271-276` needs to change from "never decides how
an entity resolves" to "the acceptance of a resolution is a deterministic, verified function;
a model may only *propose* candidates, and only as a bounded last resort." That is a change to
the paper's central selling point — **the author decides whether to adopt it, soften it, or
keep I7 out of the paper.**

**If adopted, the coordinated edits are:**
- Main text `:271-276` — qualify the determinism claim as above.
- Table 4 `:573-574` — add a last-resort proposal row, or footnote the resolution row.
- Supplement S3.6, insert after `S3_methodology_ontologies.md:157` (end of the
  `miss_raw_fallback` paragraph) — describe the last-resort LLM tier + its verification gate +
  degrade-to-raw-fallback behavior. Neighboring tone: measured, regime-scoped, honesty-forward.
- Supplement S7 — add an I7 row after `S7_improvements.md:32` and a short "already built"
  blurb under S7.3 (`:41-66`), mirroring the "bounded, verified, degrade-loud" posture used
  for I1 at `:34-39`. Verification status to record: **Implemented + unit-tested +
  live-data-verified 2026-08-12** (see `i7_realdata_verification_2026-08-12.md`); merged
  `100e35c`.

---

## 2. [actionable] Degrade-loud log-level inconsistency in the availability gate

`_llm_last_resort_resolver.py:177` logs the LLM-unavailable (unreachable-or-unpulled) branch
at `log.info`, while the reachable-but-unpulled branch logs at `log.warning` (line 170). An
unreachable endpoint is arguably the *more* severe condition, so it should not be the quieter
one. Recommend raising line 177 to `log.warning` for operator visibility, consistent with the
codebase's degrade-loud principle. One-line change; low risk.

---

## 3. [done + follow-up] Live probe of the abstain/degrade path

**Done 2026-08-12** — two nonsense tokens (`Zqxvitalis`, `Frobnicavir`) both correctly
returned `None`. A real model hallucination was caught (`Zqxvitalis` → hallucinated "Zika
virus (ZIKV)" synonyms → still abstained). See `i7_realdata_verification_2026-08-12.md`.

**Follow-up [actionable]** — the term-1 rejection was partly incidental: the hallucinated
parenthetical synonym hit a catalog `400` (transport error) rather than a pure semantic
rejection. A cleaner probe should pick a nonsense token the model maps to a real taxon via a
*non-parenthetical* synonym, so the semantic candidate-review gate is exercised in isolation.

## 5. [actionable] Parenthetical synonyms are sent to the catalog URL unsanitized

The abstain probe surfaced a latent robustness issue in `BvbrcTaxonomySearchStep`: a synonym
containing parentheses (e.g. "Zika virus (ZIKV)") is placed directly into the BV-BRC taxonomy
query URL, producing `400 Bad Request` rather than a sanitized query. Here it failed safe
(the candidate was skipped), but a *legitimate* parenthetical synonym would also 400 and be
silently dropped — a recall hole. Recommend stripping/normalizing parentheticals (or
URL-escaping the taxon_name value) before the query. Independent of I7; affects the taxonomy
search step generally.

---

## 4. [note, no action] Cache eviction is FIFO-by-write-recency

`BoundedDict` (maxsize=512) refreshes an entry's eviction slot on re-write (delete-then-insert
in `__setitem__`), so it is not strict insertion-order FIFO. This is fine for a last-resort
cache; noted only so a future reader does not mistake it for pure FIFO.
