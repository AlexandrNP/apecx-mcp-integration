"""SC-E1 builder — emit ``synonym_probe_v1.jsonl`` and report belief vs. behavior mismatches.

Hand-encoded 100 candidate queries (20 per SC-E1 scenario category)
with the author's domain-expert belief about how each *should* resolve.
The script runs each query against the currently-configured dictionary
and emits a JSONL fixture where each row records:

- the query
- the scenario category
- the actual lookup result (path + IRI + confidence) — what the
  system does TODAY
- the author's belief (path + IRI) — what it *should* do per the
  author's training-knowledge of NCBI Taxonomy
- a ``coverage_gap`` flag and free-form ``notes`` for any row where
  belief != actual

Running the script:

    .venv/bin/python scripts/build_synonym_probe.py \\
        --dict-path ~/.apecx/dictionary/dictionary.sqlite \\
        --out tests/integration/fixtures/synonym_probe_v1.jsonl

The emitted JSONL serves both as the SC-E5 calibration input and as
the regression test fixture (``tests/integration/test_synonym_probe_v1.py``).

This is a one-shot bootstrap utility, not a recurring build step.
Re-run if the dictionary substantially changes (SC-A6 ICTV, SC-B mining)
and review the diff.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Self-bootstrap matches the cli/lookup.py pattern.
if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.synonym_dictionary.enums import EntityType  # noqa: E402
from apecx_integration.synonym_dictionary.loader import (  # noqa: E402
    configure_dictionary_path,
    get_dictionary_index,
)
from apecx_integration.synonym_dictionary.lookup import lookup_entity  # noqa: E402

# OBO IRI prefix shortcut used in the candidate table for terseness.
_OBO = "http://purl.obolibrary.org/obo/"


@dataclass(frozen=True)
class Candidate:
    """One probe candidate, hand-encoded."""

    query: str
    scenario: str  # scientific_name | acronym | common_name | typo | unresolvable
    expected_path: str | tuple[str, ...]  # acceptable paths
    expected_iri: str | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Category A: scientific_name (20)  --  exact NCBI Taxonomy "scientific name"
# class hits should land on the canonical IRI with path=fast.
# ---------------------------------------------------------------------------

SCIENTIFIC = [
    Candidate(
        "Severe acute respiratory syndrome coronavirus 2",
        "scientific_name",
        "fast",
        _OBO + "NCBITaxon_2697049",
    ),
    Candidate(
        "Middle East respiratory syndrome-related coronavirus",
        "scientific_name",
        "fast",
        _OBO + "NCBITaxon_1335626",
    ),
    Candidate("Influenza A virus", "scientific_name", "fast", _OBO + "NCBITaxon_11320"),
    Candidate("Influenza B virus", "scientific_name", "fast", _OBO + "NCBITaxon_11520"),
    Candidate("Influenza C virus", "scientific_name", "fast", _OBO + "NCBITaxon_11552"),
    Candidate(
        "Human immunodeficiency virus 1", "scientific_name", "fast", _OBO + "NCBITaxon_11676"
    ),
    Candidate("Zika virus", "scientific_name", "fast", _OBO + "NCBITaxon_64320"),
    Candidate(
        "Eastern equine encephalitis virus", "scientific_name", "fast", _OBO + "NCBITaxon_11021"
    ),
    Candidate("West Nile virus", "scientific_name", "fast", _OBO + "NCBITaxon_11082"),
    Candidate("Yellow fever virus", "scientific_name", "fast", _OBO + "NCBITaxon_11089"),
    Candidate("Variola virus", "scientific_name", "fast", _OBO + "NCBITaxon_10255"),
    Candidate("Vaccinia virus", "scientific_name", "fast", _OBO + "NCBITaxon_10245"),
    Candidate("Monkeypox virus", "scientific_name", "fast", _OBO + "NCBITaxon_10244"),
    Candidate(
        "Human alphaherpesvirus 1",
        "scientific_name",
        "fast",
        _OBO + "NCBITaxon_10298",
        notes="NCBI renamed HSV-1 to Human alphaherpesvirus 1",
    ),
    Candidate("Hepatitis B virus", "scientific_name", "fast", _OBO + "NCBITaxon_10407"),
    Candidate("Tick-borne encephalitis virus", "scientific_name", "fast", _OBO + "NCBITaxon_11084"),
    Candidate("Japanese encephalitis virus", "scientific_name", "fast", _OBO + "NCBITaxon_11072"),
    Candidate(
        "Rabies lyssavirus",
        "scientific_name",
        "fast",
        _OBO + "NCBITaxon_11292",
        notes="NCBI species name for Rabies virus",
    ),
    Candidate(
        "Measles morbillivirus",
        "scientific_name",
        "fast",
        _OBO + "NCBITaxon_11234",
        notes="NCBI renamed Measles virus to Measles morbillivirus",
    ),
    Candidate(
        "Hepacivirus C",
        "scientific_name",
        "fast",
        _OBO + "NCBITaxon_11103",
        notes="ICTV name for HCV species; NCBI canonical now Orthohepacivirus hominis",
    ),
]


# ---------------------------------------------------------------------------
# Category B: acronym (20)  --  NCBI acronym-class rows should hit fast.
# Several entries below are KNOWN GAPS where the system misses today
# (CHIKV, several common acronyms NCBI doesn't carry as standalone rows) —
# these are SC-B / SC-A6 ICTV targets.
# ---------------------------------------------------------------------------

ACRONYM = [
    Candidate("EEEV", "acronym", "fast", _OBO + "NCBITaxon_11021"),
    Candidate("ZIKV", "acronym", "fast", _OBO + "NCBITaxon_64320"),
    Candidate("WNV", "acronym", "fast", _OBO + "NCBITaxon_11082"),
    Candidate(
        "H1N1",
        "acronym",
        "fast",
        _OBO + "NCBITaxon_114727",
        notes="NCBI taxon for H1N1 subtype, not Influenza A species",
    ),
    Candidate(
        "HCV",
        "acronym",
        "fast",
        _OBO + "NCBITaxon_3052230",
        notes="NCBI canonical: Orthohepacivirus hominis",
    ),
    Candidate("SARS-CoV-2", "acronym", "fast", _OBO + "NCBITaxon_2697049"),
    Candidate("MERS-CoV", "acronym", "fast", _OBO + "NCBITaxon_1335626"),
    Candidate("HIV-1", "acronym", "fast", _OBO + "NCBITaxon_11676"),
    Candidate("HIV-2", "acronym", "fast", _OBO + "NCBITaxon_11709"),
    Candidate("HSV-1", "acronym", "fast", _OBO + "NCBITaxon_10298"),
    Candidate("HSV-2", "acronym", "fast", _OBO + "NCBITaxon_10310"),
    Candidate("VZV", "acronym", "fast", _OBO + "NCBITaxon_10335"),
    Candidate("EBV", "acronym", "fast", _OBO + "NCBITaxon_10376"),
    Candidate("CMV", "acronym", "fast", _OBO + "NCBITaxon_10359", notes="Human cytomegalovirus"),
    Candidate("YFV", "acronym", "fast", _OBO + "NCBITaxon_11089"),
    Candidate("JEV", "acronym", "fast", _OBO + "NCBITaxon_11072"),
    Candidate(
        "RSV",
        "acronym",
        "ambiguous",
        None,
        notes="6 candidates per SC-A5b: Bovine/Human/Ovine "
        "orthopneumovirus + Rous sarcoma + Tenuivirus + clade",
    ),
    Candidate(
        "DENV",
        "acronym",
        ("fast", "ambiguous"),
        None,
        notes="DENV may resolve to species or be ambiguous across 4 serotypes",
    ),
    Candidate(
        "CHIKV",
        "acronym",
        "fast",
        _OBO + "NCBITaxon_37124",
        notes="KNOWN GAP — NCBI lacks acronym row; SC-B corpus-mining target",
    ),
    Candidate(
        "MPXV",
        "acronym",
        "fast",
        _OBO + "NCBITaxon_10244",
        notes="Monkeypox acronym — may be a gap",
    ),
]


# ---------------------------------------------------------------------------
# Category C: common_name (20)  --  NCBI common-name/genbank-common-name rows.
# ---------------------------------------------------------------------------

COMMON = [
    Candidate(
        "Marburg virus",
        "common_name",
        ("fast", "ambiguous"),
        None,
        notes="May surface as common name on Orthomarburgvirus marburgense",
    ),
    Candidate("yellow fever virus", "common_name", "fast", _OBO + "NCBITaxon_11089"),
    Candidate(
        "dengue virus",
        "common_name",
        ("fast", "ambiguous"),
        None,
        notes="Dengue is a species complex",
    ),
    Candidate("monkeypox virus", "common_name", "fast", _OBO + "NCBITaxon_10244"),
    Candidate("measles virus", "common_name", "fast", _OBO + "NCBITaxon_11234"),
    Candidate(
        "Ebola virus",
        "common_name",
        ("fast", "ambiguous"),
        None,
        notes="Multiple ebolavirus species; common name may be ambiguous",
    ),
    Candidate(
        "smallpox virus",
        "common_name",
        "fast",
        _OBO + "NCBITaxon_10255",
        notes="NCBI common name for Variola virus",
    ),
    Candidate("rabies virus", "common_name", "fast", _OBO + "NCBITaxon_11292"),
    Candidate(
        "herpes simplex virus",
        "common_name",
        ("ambiguous", "fast"),
        None,
        notes="HSV-1 vs HSV-2 — likely ambiguous",
    ),
    Candidate(
        "cytomegalovirus", "common_name", ("fast", "ambiguous"), None, notes="Genus across hosts"
    ),
    Candidate(
        "respiratory syncytial virus",
        "common_name",
        "ambiguous",
        None,
        notes="Like RSV — multi-host species",
    ),
    Candidate("Lassa virus", "common_name", "fast", _OBO + "NCBITaxon_11620"),
    Candidate("rubella virus", "common_name", "fast", _OBO + "NCBITaxon_11041"),
    Candidate(
        "mumps virus",
        "common_name",
        "fast",
        _OBO + "NCBITaxon_1979160",
        notes="NCBI species name: Orthorubulavirus parotitidis",
    ),
    Candidate("Japanese encephalitis virus", "common_name", "fast", _OBO + "NCBITaxon_11072"),
    Candidate("West Nile virus", "common_name", "fast", _OBO + "NCBITaxon_11082"),
    Candidate("Epstein-Barr virus", "common_name", "fast", _OBO + "NCBITaxon_10376"),
    Candidate("Lassa mammarenavirus", "common_name", "fast", _OBO + "NCBITaxon_11620"),
    Candidate("varicella-zoster virus", "common_name", "fast", _OBO + "NCBITaxon_10335"),
    Candidate(
        "Marburg marburgvirus",
        "common_name",
        "fast",
        None,
        notes="ICTV old species; mapped to Orthomarburgvirus marburgense",
    ),
]


# ---------------------------------------------------------------------------
# Category D: typo (20)  --  1-2 char edits, missing punctuation, etc.
# Expected paths derive from the trigram-Jaccard math:
#   - very long canonical (>=30 chars), 1-char edit → fuzzy (≥0.85)
#   - medium-length (15-30 chars), 1-char edit → ambiguous (0.70-0.85)
#   - short canonicals (<15 chars), most edits → miss (<0.70)
# ---------------------------------------------------------------------------

TYPO = [
    Candidate(
        "Severe acute respiratory syndrom coronavirus 2",
        "typo",
        "fuzzy",
        _OBO + "NCBITaxon_2697049",
        notes="1-char drop on 45-char canonical → Jaccard 0.90",
    ),
    Candidate(
        "Hepatitus C virus",
        "typo",
        "ambiguous",
        None,
        notes="1-char swap on 17-char string → Jaccard 0.76",
    ),
    Candidate(
        "Severe acute respiratory syndrom coronavirus", "typo", "fuzzy", _OBO + "NCBITaxon_694009"
    ),
    Candidate(
        "Middle East respiratory syndrom-related coronavirus",
        "typo",
        "fuzzy",
        _OBO + "NCBITaxon_1335626",
    ),
    Candidate("Human immunodeficiency vrius 1", "typo", "fuzzy", _OBO + "NCBITaxon_11676"),
    Candidate(
        "Human immunodeficiency virus  1",
        "typo",
        "fast",
        _OBO + "NCBITaxon_11676",
        notes="Extra space — normalization should collapse it",
    ),
    Candidate(
        "Zika virus.",
        "typo",
        ("fast", "fuzzy", "ambiguous"),
        None,
        notes="Trailing punctuation; depends on normalization",
    ),
    Candidate(
        "zika  virus",
        "typo",
        "fast",
        _OBO + "NCBITaxon_64320",
        notes="Double space; normalize collapses",
    ),
    Candidate("Influenza A virsu", "typo", "ambiguous", None, notes="rs swap, short canonical"),
    Candidate("Tick-bourne encephalitis virus", "typo", "ambiguous", None, notes="o→ou typo"),
    Candidate(
        "Japaneese encephalitis virus",
        "typo",
        "ambiguous",
        None,
        notes="Extra e — 27-char canonical",
    ),
    Candidate(
        "Yelow fever virus",
        "typo",
        ("ambiguous", "miss"),
        None,
        notes="Drop l from yellow; 17 chars",
    ),
    Candidate("monkeypoxvirus", "typo", ("ambiguous", "miss"), None, notes="Drop space"),
    Candidate("Eastern equine encephalitis vrius", "typo", "fuzzy", _OBO + "NCBITaxon_11021"),
    Candidate(
        "Tick borne encephalitis virus", "typo", ("fast", "fuzzy"), None, notes="Hyphen → space"
    ),
    Candidate(
        "Severe acute respiratory syndrome coronaviurs 2",
        "typo",
        "fuzzy",
        _OBO + "NCBITaxon_2697049",
        notes="rs swap on virus, 47-char query",
    ),
    Candidate("Variolaa virus", "typo", ("ambiguous", "miss"), None, notes="Extra a"),
    Candidate("Yelo fever virus", "typo", ("ambiguous", "miss"), None, notes="Multiple edits"),
    Candidate(
        "Inflenza A virus", "typo", ("ambiguous", "miss"), None, notes="Drop u from influenza"
    ),
    Candidate("Rabbies lyssavirus", "typo", "ambiguous", None, notes="Double-b typo"),
]


# ---------------------------------------------------------------------------
# Category E: unresolvable (20)  --  pure noise; must return miss.
# Any non-miss here is a false-positive that bursts the AMBIGUOUS HITL
# queue with garbage. Calibration uses this category for FP measurement.
# ---------------------------------------------------------------------------

UNRESOLVABLE = [
    Candidate("asdfghjkl", "unresolvable", "miss"),
    Candidate("xyzzyx", "unresolvable", "miss"),
    Candidate("zzzzz virus", "unresolvable", "miss"),
    Candidate("blablabla", "unresolvable", "miss"),
    Candidate("quux", "unresolvable", "miss"),
    Candidate("nonsense", "unresolvable", "miss"),
    Candidate("madeupvirus2025", "unresolvable", "miss"),
    Candidate("potato unicorn dragon", "unresolvable", "miss"),
    Candidate("fictional virus that doesnt exist", "unresolvable", "miss"),
    Candidate("supercalifragilisticexpialidocious", "unresolvable", "miss"),
    Candidate("abc123 random nonsense", "unresolvable", "miss"),
    Candidate("Q9zX random string", "unresolvable", "miss"),
    Candidate("xyz12345", "unresolvable", "miss"),
    Candidate("rrrrr", "unresolvable", "miss"),
    Candidate("12345 virus 67890", "unresolvable", "miss"),
    Candidate("fake unicorn pathogen", "unresolvable", "miss"),
    Candidate("ghostbuster virus", "unresolvable", "miss"),
    Candidate("dragonbreath syndrome", "unresolvable", "miss"),
    Candidate("totally invented pathogen alpha gamma", "unresolvable", "miss"),
    Candidate("klingon hemorrhagic fever", "unresolvable", "miss"),
]


# SC-E5b (2026-06-08): adversarial biology-adjacent noise.
# Distinct scenario label from ``unresolvable`` so the strict
# "pure noise must miss" invariant in test_synonym_probe_v1.py stays
# enforceable on the original 20 probes; the adversarial set is for
# FPR calibration, not for a hard pass/fail gate.
# These are real biological terms (gene names, plant/bacteria species,
# cell lines, organ tissues) that share trigram overlap with virus names
# but are NOT viruses. Calibration uses these to surface false-positive
# risk that the original 20 noise probes (pure gibberish) can't detect.
# Honest expectation: a few of these may legitimately resolve because
# their string genuinely overlaps with a real virus-subtree taxon (e.g.,
# a host species mentioned by name in a virus's "includes" row). Those
# are NOT bugs — but they are calibration signal about what counts as
# "noise" vs. "valid alternate lookup".
ADVERSARIAL = [
    # Real but unrelated species (bacteria, plants, fungi — NOT viruses)
    Candidate(
        "Saccharomyces cerevisiae",
        "adversarial_noise",
        "miss",
        notes="brewers yeast; out-of-subtree",
    ),
    Candidate("Escherichia coli", "adversarial_noise", "miss", notes="bacterium; out-of-subtree"),
    Candidate("Arabidopsis thaliana", "adversarial_noise", "miss", notes="plant; out-of-subtree"),
    Candidate(
        "Drosophila melanogaster", "adversarial_noise", "miss", notes="fruit fly; out-of-subtree"
    ),
    Candidate("Mus musculus", "adversarial_noise", "miss", notes="mouse; out-of-subtree"),
    Candidate(
        "Caenorhabditis elegans", "adversarial_noise", "miss", notes="nematode; out-of-subtree"
    ),
    Candidate(
        "Schistosoma mansoni", "adversarial_noise", "miss", notes="parasitic worm; not a virus"
    ),
    Candidate(
        "Plasmodium falciparum", "adversarial_noise", "miss", notes="malaria parasite; not a virus"
    ),
    Candidate(
        "Mycobacterium tuberculosis", "adversarial_noise", "miss", notes="bacterium; not a virus"
    ),
    Candidate("Staphylococcus aureus", "adversarial_noise", "miss", notes="bacterium"),
    Candidate("Candida albicans", "adversarial_noise", "miss", notes="fungus"),
    Candidate("Aspergillus fumigatus", "adversarial_noise", "miss", notes="fungus"),
    # Human gene names (some symbols collide with virus acronyms)
    Candidate("BRCA1", "adversarial_noise", "miss", notes="human breast cancer gene"),
    Candidate("TP53", "adversarial_noise", "miss", notes="human tumor suppressor gene"),
    Candidate("EGFR", "adversarial_noise", "miss", notes="epidermal growth factor receptor"),
    Candidate(
        "CD4",
        "adversarial_noise",
        "miss",
        notes="human T cell marker; relevant to HIV but not itself a virus",
    ),
    Candidate(
        "ACE2", "adversarial_noise", "miss", notes="human SARS-CoV-2 receptor; receptor not virus"
    ),
    Candidate("NF-kB", "adversarial_noise", "miss", notes="human transcription factor"),
    # Cell lines + tissues
    Candidate("HeLa", "adversarial_noise", "miss", notes="human cervical cell line"),
    Candidate("HEK293", "adversarial_noise", "miss", notes="human embryonic kidney cell line"),
    Candidate("Vero cells", "adversarial_noise", "miss", notes="monkey kidney cell line"),
    Candidate("hepatocyte", "adversarial_noise", "miss", notes="liver cell type"),
    Candidate("epithelial cell", "adversarial_noise", "miss"),
    # Partial / truncated pathogen strings (adversarial: share trigrams
    # with virus names but don't constitute a query a user would type
    # expecting a single answer)
    Candidate(
        "virus",
        "adversarial_noise",
        "miss",
        notes="bare word; matches >>1000 taxa, should NOT resolve",
    ),
    Candidate(
        "coronavirus",
        "adversarial_noise",
        "miss",
        notes="bare genus; legitimately ambiguous across many species",
    ),
    Candidate("flavivirus", "adversarial_noise", "miss", notes="bare genus"),
    Candidate("strain", "adversarial_noise", "miss", notes="bare descriptor"),
    Candidate("respiratory", "adversarial_noise", "miss", notes="bare adjective"),
    Candidate(
        "hemorrhagic fever",
        "adversarial_noise",
        "miss",
        notes="disease descriptor not pinned to a single pathogen",
    ),
    Candidate("encephalitis", "adversarial_noise", "miss", notes="disease descriptor"),
    Candidate(
        "Coronaviridae",
        "adversarial_noise",
        "miss",
        notes="family-level — debatable; may legitimately resolve",
    ),
    Candidate("Filoviridae", "adversarial_noise", "miss", notes="family-level"),
    # Diseases that aren't viral
    Candidate("Lyme disease", "adversarial_noise", "miss", notes="bacterial (Borrelia)"),
    Candidate("tetanus", "adversarial_noise", "miss", notes="bacterial (Clostridium)"),
    Candidate("cholera", "adversarial_noise", "miss", notes="bacterial (Vibrio)"),
    Candidate("syphilis", "adversarial_noise", "miss", notes="bacterial (Treponema)"),
    Candidate("anthrax", "adversarial_noise", "miss", notes="bacterial (Bacillus)"),
    # Chemicals / drugs sometimes typed into virus search boxes
    Candidate("remdesivir", "adversarial_noise", "miss", notes="antiviral drug; not a virus"),
    Candidate("ivermectin", "adversarial_noise", "miss", notes="antiparasitic drug"),
    Candidate("aspirin", "adversarial_noise", "miss"),
    # Mostly-noise that has SOME bio overlap
    Candidate("virion", "adversarial_noise", "miss", notes="bare technical term, not a taxon"),
    Candidate("capsid protein", "adversarial_noise", "miss", notes="virus component, not a virus"),
    Candidate("glycoprotein", "adversarial_noise", "miss", notes="generic molecule type"),
    Candidate("polymerase", "adversarial_noise", "miss", notes="enzyme name"),
    Candidate("RNA virus", "adversarial_noise", "miss", notes="grammatical category, not a taxon"),
    Candidate("DNA virus", "adversarial_noise", "miss", notes="grammatical category"),
    Candidate(
        "retrovirus", "adversarial_noise", "miss", notes="genus name; legitimately ambiguous"
    ),
    Candidate("alphavirus", "adversarial_noise", "miss", notes="genus name"),
    Candidate("orthomyxovirus", "adversarial_noise", "miss"),
    Candidate("paramyxovirus", "adversarial_noise", "miss"),
    Candidate("animal virus", "adversarial_noise", "miss"),
    Candidate("plant virus", "adversarial_noise", "miss"),
]


ALL_CANDIDATES = SCIENTIFIC + ACRONYM + COMMON + TYPO + UNRESOLVABLE + ADVERSARIAL


def _paths_accepted(expected: str | tuple[str, ...]) -> tuple[str, ...]:
    return (expected,) if isinstance(expected, str) else expected


def _belief_matches(cand: Candidate, actual_path: str, actual_iri: str | None) -> bool:
    """True iff the actual lookup honors the candidate's belief.

    Path must match one of the accepted alternatives. If the belief carries
    an expected_iri, the actual canonical_iri must match (only enforced for
    single-candidate paths — ambiguous/miss don't carry one).
    """
    if actual_path not in _paths_accepted(cand.expected_path):
        return False
    if cand.expected_iri is None:
        return True
    if actual_path in ("ambiguous", "miss", "deleted"):
        return True  # IRI not applicable
    return actual_iri == cand.expected_iri


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_synonym_probe")
    parser.add_argument("--dict-path", type=Path, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/integration/fixtures/synonym_probe_v1.jsonl"),
    )
    args = parser.parse_args(argv)

    dict_path = args.dict_path or Path(
        os.environ.get(
            "APECX_SYNONYM_DICT_PATH",
            str(Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"),
        )
    )
    if not dict_path.exists():
        print(f"ERROR: dict not found at {dict_path}", file=sys.stderr)
        return 1
    configure_dictionary_path(dict_path)
    _, err = get_dictionary_index()
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    mismatch_count = 0
    scenario_counts: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    for cand in ALL_CANDIDATES:
        scenario_counts[cand.scenario] += 1
        result = lookup_entity(cand.query, entity_type=EntityType.PATHOGEN)
        actual_path = result.path
        actual_iri = result.canonical_iri
        path_counts[actual_path] += 1
        belief_ok = _belief_matches(cand, actual_path, actual_iri)
        coverage_gap = not belief_ok
        if coverage_gap:
            mismatch_count += 1
        row = {
            "query": cand.query,
            "scenario": cand.scenario,
            "actual_path": actual_path,
            "actual_iri": actual_iri,
            "actual_confidence": round(result.confidence, 4),
            "actual_resolution_status": result.resolution_status.value,
            "actual_candidate_count": len(result.candidates),
            "expected_path": list(_paths_accepted(cand.expected_path)),
            "expected_iri": cand.expected_iri,
            "coverage_gap": coverage_gap,
            "notes": cand.notes,
        }
        rows.append(row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    print(f"wrote {len(rows)} probes to {args.out}")
    print(f"  scenarios: {dict(scenario_counts)}")
    print(f"  actual paths: {dict(path_counts)}")
    print(f"  belief != actual: {mismatch_count}/{len(rows)}")
    if mismatch_count > 0:
        print("\nmismatches (belief in candidate table vs. system behavior):")
        for row in rows:
            if not row["coverage_gap"]:
                continue
            exp = "|".join(row["expected_path"])
            exp_iri = row["expected_iri"] or "-"
            act_iri = row["actual_iri"] or "-"
            exp_tail = exp_iri.rsplit("_", 1)[-1] if exp_iri != "-" else "-"
            act_tail = act_iri.rsplit("_", 1)[-1] if act_iri != "-" else "-"
            print(
                f"  [{row['scenario']:18s}] {row['query']!r:55s}"
                f" expected={exp:18s}({exp_tail:10s})"
                f" actual={row['actual_path']:10s}({act_tail})"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
