"""Project A Step 3a — the contract WARN-ratchet.

Asserts the number of DirectLink boundaries lacking both-endpoint contract coverage never
INCREASES past a committed baseline. As contributors annotate workflows the count drops and
the baseline is lowered — it can only ratchet DOWN. A PR that adds an unannotated boundary
fails this test; that is the mechanism that makes corpus coverage monotonically improve.

Lower BASELINE whenever you add contracts (never raise it without a documented reason).
"""

from __future__ import annotations

import yaml

from apecx_integration.composition.contract_coverage import (
    count_undeclared,
    undeclared_boundaries,
)

# Undeclared-boundary count: 142 (pre-3a) -> 141 (3a exemplar) -> 138 (3b batch1: code_write/
# code_review/test_write/entity_extraction) -> 135 (3b batch2: isolated_py_exec/bug_fix_write/
# code_document_write/memory_write) -> 134 (3b batch3: code_reflection/code_verification/
# code_with_tests subworkflow steps) -> 126 (3c: workflow-level DUs in the code_verification/
# code_reflection/code_with_tests inner workflows, pairing with the annotated wrappers).
# RATCHET: only ever lower this. If it would rise, a boundary lost coverage — annotate, don't bump.
BASELINE = 126

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_LINK = "nanobrain.core.link.DirectLink"


def test_corpus_undeclared_boundaries_within_baseline():
    n = count_undeclared()
    assert n <= BASELINE, (
        f"undeclared contract boundaries ROSE to {n} (baseline {BASELINE}). A new/changed link "
        f"boundary lacks both-endpoint contracts — annotate it (see docs/contract_authoring.md), "
        f"don't raise the baseline. Current undeclared: {undeclared_boundaries()[:5]}..."
    )


def test_baseline_is_tight():
    # Guard against a stale (too-high) baseline silently absorbing regressions: the baseline
    # must equal the live count (lower it whenever you annotate).
    assert count_undeclared() == BASELINE, (
        f"baseline {BASELINE} != live count {count_undeclared()} — update BASELINE to the live "
        f"count after annotating (the ratchet must stay tight)."
    )


def test_exemplar_boundary_is_covered_and_compatible():
    # The Step 3a exemplar: assembly->synthesis must be both-declared (covered) AND compatible.
    from pathlib import Path

    from nanobrain.core.data_contract import compatible, parse_contract

    import apecx_integration

    steps = (
        Path(apecx_integration.__file__).parent / "composition/workflows/rag_e2e_synthesis/steps"
    )
    prod = yaml.safe_load((steps / "synthesis_context_assembly.yml").read_text())[
        "output_data_units"
    ]["synthesis_bundle_output"]["contract"]
    cons = yaml.safe_load((steps / "rag_synthesis.yml").read_text())["input_data_units"][
        "synthesis_input"
    ]["contract"]
    ok, reason = compatible(parse_contract(prod), parse_contract(cons))
    assert ok, f"exemplar assembly->synthesis must be compatible; got {reason!r}"
    # And it is no longer in the undeclared set.
    assert not [b for b in undeclared_boundaries() if "synthesis_bundle_output" in b[1]], (
        "assembly->synthesis should be covered after Step 3a annotation"
    )


def test_3b_code_write_boundaries_compatible():
    # N2 hardening: the ratchet counts presence (both-declared), not compatibility — so assert
    # the 3b code_write boundaries are actually compatible (a both-declared-but-incompatible
    # boundary would drop the count + pass the ratchet otherwise).
    from pathlib import Path

    from nanobrain.core.data_contract import compatible, parse_contract

    import apecx_integration

    steps = Path(apecx_integration.__file__).parent / "composition/workflows/code_writing/steps"
    prod = yaml.safe_load((steps / "code_write.yml").read_text())["output_data_units"][
        "code_write_output"
    ]["contract"]
    for cons_f, cons_du in [
        ("code_review.yml", "code_review_input"),
        ("test_write.yml", "test_write_input"),
    ]:
        cc = yaml.safe_load((steps / cons_f).read_text())["input_data_units"][cons_du]["contract"]
        ok, why = compatible(parse_contract(prod), parse_contract(cc))
        assert ok, f"code_write_output -> {cons_du} must be compatible; got {why!r}"


def _wf(tmp_path, name, *, with_contracts):
    c = "\n      contract: {kind: text}" if with_contracts else ""
    (tmp_path / f"{name}.yml").write_text(
        f"name: {name}\n"
        f"steps:\n  dummy: {{class: X, config: d.yml}}\n"
        f'links:\n  l:\n    class: "{_LINK}"\n'
        f"    config: {{link_type: direct, source: workflow_input, target: workflow_output}}\n"
        f'input_data_units:\n  workflow_input:\n    class: "{_DU}"\n    name: workflow_input{c}\n'
        f'output_data_units:\n  workflow_output:\n    class: "{_DU}"\n    name: workflow_output{c}\n',
        encoding="utf-8",
    )


def test_counter_logic_on_synthetic_corpus(tmp_path):
    # One covered (both DUs declare a contract) + one undeclared -> count == 1, pinned
    # independently of the real corpus.
    _wf(tmp_path, "covered", with_contracts=True)
    _wf(tmp_path, "bare", with_contracts=False)
    boundaries = undeclared_boundaries(roots=[], wf_dir=tmp_path)
    assert len(boundaries) == 1
    assert boundaries[0][0] == "bare.yml"
