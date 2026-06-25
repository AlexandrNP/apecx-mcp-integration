"""Reason-aware checks for the viral_epitope_analysis eval. PURE — operate on data captured from a real
run (step events, the on-disk artifact dir, the report markdown, the proceed-notes).

THE central design point (learned from a grounding run): an empty content artifact is NOT automatically a
bug. viral_epitope_analysis degrades LOUDLY — when the sequence-conservation leg can't run (e.g. no protein
in the query), it writes `conserved_regions: []` AND a proceed_note explaining why + the next action. That
is correct reliability behavior, not a silent failure. So the full-artifacts check is REASON-AWARE: an
empty content artifact is OK iff a proceed_note covers its stage; empty + SILENCE is the real bug.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    passed: bool
    evidence: str


# Content artifacts that should carry real data on EVERY run (the network legs — no degrade excuse).
_ALWAYS_CONTENT = ("structural_records", "publications")
# Content artifacts gated on inputs/infra — empty is OK iff a proceed_note covers the stage.
# stage substring (in proceed_notes) that excuses each:
_GATED_CONTENT = {
    "conserved_regions": "sequence conservation",
    "rhea_conservation": "rhea",
    "cross_clade_breadth": "sequence conservation",
}


def check_streaming(expected_steps, events_by_step) -> CheckResult:
    """Each expected step emitted >=1 StepEvent (the pipeline streams to the user)."""
    expected = set(expected_steps)
    silent = expected - set(events_by_step)
    return CheckResult(
        "streaming",
        bool(expected) and not silent,
        f"streamed {len(expected) - len(silent)}/{len(expected)}; silent={sorted(silent)[:5]}",
    )


def check_completeness(expected_steps, events_by_step) -> CheckResult:
    """Each expected step reached step_complete (none stuck/failed)."""
    expected = set(expected_steps)
    incomplete = [s for s in expected if "step_complete" not in events_by_step.get(s, set())]
    # Only TOP-LEVEL (expected) step failures count. A NESTED step that failed but was caught by its
    # degrade-loud parent (e.g. muscle_alignment inside the rhea_genomic leg) is honest degradation, not a
    # run failure — consistent with run_workflow's refined G127 honesty check (finding EF7).
    failed = [s for s in expected if "step_failed" in events_by_step.get(s, set())]
    return CheckResult(
        "completeness",
        bool(expected) and not incomplete and not failed,
        f"complete {len(expected) - len(incomplete)}/{len(expected)}; "
        f"incomplete={sorted(incomplete)[:4]} failed={failed[:4]}",
    )


def _nonempty_json(path: Path) -> bool:
    """A tool_outputs JSON carries real content (not [] / {} / null / missing)."""
    if not path.is_file():
        return False
    try:
        v = json.loads(path.read_text())
    except Exception:
        return path.stat().st_size > 0
    return bool(v)  # [] {} "" 0 None -> empty


def check_full_artifacts(run_dir: Path, proceed_stages: set[str]) -> CheckResult:
    """report.md + data.json non-empty; the ALWAYS-content tool_outputs non-empty; a GATED-content empty
    artifact is OK iff a proceed_note covers its stage; figures present OR a sequence degrade. The 'full
    artifacts count' = the number of non-empty artifacts written. A SILENT empty (no proceed_note) FAILS."""
    run_dir = Path(run_dir)
    problems: list[str] = []
    report = run_dir / "report.md"
    if not (report.is_file() and report.read_text().strip()):
        problems.append("report.md empty/missing")
    data = run_dir / "data.json"
    if not (data.is_file() and data.stat().st_size > 2):
        problems.append("data.json empty/missing")

    tool_dir = run_dir / "tool_outputs"
    for name in _ALWAYS_CONTENT:
        if not _nonempty_json(tool_dir / f"{name}.json"):
            problems.append(f"{name} EMPTY (no degrade excuse — a silent failure)")
    for name, stage_kw in _GATED_CONTENT.items():
        p = tool_dir / f"{name}.json"
        if p.is_file() and not _nonempty_json(p) and not any(stage_kw in s for s in proceed_stages):
            problems.append(f"{name} EMPTY + NO proceed_note for '{stage_kw}' (silent failure)")

    figures = list((run_dir / "figures").glob("*.png")) if (run_dir / "figures").is_dir() else []
    seq_degraded = any("sequence conservation" in s for s in proceed_stages)
    if not figures and not seq_degraded:
        problems.append("no figures + no sequence degrade (a figure should exist)")

    nonempty_count = sum(1 for p in run_dir.rglob("*") if p.is_file() and p.stat().st_size > 2)
    return CheckResult(
        "full_artifacts",
        not problems,
        f"nonempty_artifacts={nonempty_count} figures={len(figures)} "
        f"degraded_stages={sorted(proceed_stages)[:3]} problems={problems[:3]}",
    )


_CONTRACT_SECTIONS = (
    "# Answer",
    "## Data actually used",
    "## Cross-referenced epitope candidates",
    "## Structural evidence",
    "## Evidence coverage",
    "## Sources and evidence",
)
_CITATION_MARKERS = (
    r"\[BV-BRC genome [^\]\s]+\]",
    r"\[VIOLIN [^\]\s]+\]",
    r"\[RAG chunk #\d+\]",
    r"\[Globus [^\]\s]+\]",
    r"\[10\.[0-9]+/[^\]\s]+\]",
    r"\bDOI[: ]",
)


def check_report_references(markdown: str | None) -> CheckResult:
    """The report carries the contract sections AND real references (a citation marker / a titled source)."""
    md = markdown or ""
    missing = [s for s in _CONTRACT_SECTIONS if s not in md]
    citations = sum(len(re.findall(p, md)) for p in _CITATION_MARKERS)
    # the Sources ledger should have >=1 titled record (not all "(untitled)")
    src = md[md.find("## Sources and evidence") :] if "## Sources and evidence" in md else ""
    titled = bool(src) and ("(untitled)" not in src or src.count("- ") > src.count("(untitled)"))
    passed = not missing and citations > 0 and titled
    return CheckResult(
        "report_references",
        passed,
        f"missing_sections={missing[:3]} citations={citations} has_titled_sources={titled}",
    )


def protabank_count(markdown: str | None) -> int | None:
    """The ProtaBank availability count parsed from the report ('protabank: N available / M used'), or
    None if ProtaBank is not even reported (a silent omission). Used by the loop for the cross-virus
    never-retrieved verdict."""
    md = markdown or ""
    m = re.search(r"protabank.{0,40}?(\d+)\s*available", md, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.search(r"protabank", md, re.IGNORECASE):
        return 0  # mentioned but no count → treat as 0-but-reported
    return None  # not reported at all


def check_protabank_reported(markdown: str | None) -> CheckResult:
    """ProtaBank MUST be REPORTED (count present, no silent omission). Whether it's >0 is a cross-virus
    verdict the loop computes; a single run only requires it not be silently dropped."""
    n = protabank_count(markdown)
    return CheckResult(
        "protabank_reported",
        n is not None,
        f"protabank_count={n} ({'reported' if n is not None else 'SILENTLY OMITTED'})",
    )


def harmonization_imprecise_indices(markdown: str | None) -> list[str]:
    """The indices the report DISCLOSES as taxon-IMPRECISE (raw free-text fallback, taxon-IRI leg empty).
    Parsed from the data_readiness disclosure the product now emits."""
    return re.findall(r"(\w+): \d+ record\(s\) via taxon-IMPRECISE", markdown or "")


def check_harmonization_disclosed(markdown: str | None) -> CheckResult:
    """INFORMATIONAL quality signal: how many indices are taxon-IMPRECISE (the taxon-IRI harmonization
    returned nothing, so the count is an un-taxon-filtered free-text fallback). The product now DISCLOSING
    this is the fix (EF4: e.g. influenza's species-vs-strain taxid mismatch breaks the filter on ~5 of 9
    indices). Always passes — it surfaces the count so a degraded-harmonization virus is visible, not a
    clean-looking miss."""
    idx = harmonization_imprecise_indices(markdown)
    return CheckResult(
        "harmonization_disclosed",
        True,
        f"taxon-imprecise indices disclosed: {len(idx)} ({idx[:5]})",
    )
