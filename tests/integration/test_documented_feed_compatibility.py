"""Low-noise composer-bug detector: documented catalog 'feeds' must be contract-compatible.

The two composer bugs this session (entity_extraction->assembly, analysis->summarize) shared a
signature: a catalog wrapper DOCUMENTS that its output feeds a specific consumer's input DU, but
the output's declared shape does NOT satisfy that consumer's input contract — so the composer's
natural wiring fails at runtime (hidden by status-not-output tests, G127).

This detector auto-finds those documented feeds — a producer wrapper whose description NAMES another
component's input data-unit — and asserts the producer's output contract is compatible with that
consumer's input contract. Unlike a blind pairwise scan (which is dominated by nonsensical pairs),
this keys on the *documented* coupling, so a hit is a real composer-bug risk. As contributors add
components that document feeds, this checks them automatically. Pure static analysis, no LLM.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

import yaml
from nanobrain.core.data_contract import compatible, parse_contract

import apecx_integration

_COMP = Path(apecx_integration.__file__).parent / "composition"
_CATALOG_GLOBS = [
    str(_COMP / "_catalog_steps/*.yml"),
    str(_COMP / "workflows/code_writing/steps/*.yml"),
    str(_COMP / "workflows/rag_e2e_synthesis/steps/*.yml"),
]


def _collect():
    """inputs: {input_du_name: (component, contract)}; outputs: [(component, out_du, contract, desc_text)]."""
    inputs: dict[str, tuple[str, dict]] = {}
    outputs: list[tuple[str, str, dict, str]] = []
    for g in _CATALOG_GLOBS:
        for f in glob.glob(g):
            d = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
            if not isinstance(d, dict):
                continue
            comp = d.get("name", Path(f).stem)
            top_desc = str(d.get("description", ""))
            for du, spec in (d.get("input_data_units") or {}).items():
                if isinstance(spec, dict) and spec.get("contract"):
                    inputs[du] = (comp, spec["contract"])
            for du, spec in (d.get("output_data_units") or {}).items():
                if isinstance(spec, dict) and spec.get("contract"):
                    desc = f"{top_desc} {spec.get('description', '')} " + Path(f).read_text(
                        encoding="utf-8"
                    )
                    outputs.append((comp, du, spec["contract"], desc))
    return inputs, outputs


def _documented_feeds():
    """(producer, out_du, consumer, in_du, ocontract, icontract) for each documented feed:
    a producer output whose wrapper text NAMES another component's contract-bearing input DU."""
    inputs, outputs = _collect()
    feeds = []
    for comp, odu, ocontract, desc in outputs:
        for idu, (icomp, icontract) in inputs.items():
            if icomp == comp:
                continue
            # word-boundary match so 'assembly_input' doesn't match a substring accidentally.
            if re.search(rf"\b{re.escape(idu)}\b", desc):
                feeds.append((comp, odu, icomp, idu, ocontract, icontract))
    return feeds


def test_documented_feeds_exist():
    # Guard the detector itself: at least the known entity_extraction->assembly feed is detected
    # (its wrapper documents feeding assembly_input). If this drops to 0 the detector has gone blind.
    feeds = _documented_feeds()
    assert any(idu == "assembly_input" for _, _, _, idu, _, _ in feeds), (
        f"detector found no entity_extraction->assembly feed; detected: "
        f"{[(c, i) for c, _, _, i, _, _ in feeds]}"
    )


def test_documented_feeds_are_compatible():
    violations = []
    for producer, odu, consumer, idu, oc, ic in _documented_feeds():
        ok, why = compatible(parse_contract(oc), parse_contract(ic))
        if not ok:
            violations.append(f"{producer}.{odu} -> {consumer}.{idu}: {why}")
    assert not violations, (
        "documented catalog feeds are contract-incompatible (composer-bug risk — a wrapper claims "
        "to feed a consumer its output cannot satisfy):\n  " + "\n  ".join(violations)
    )
