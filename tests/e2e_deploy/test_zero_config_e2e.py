"""End-to-end zero-config execution test — CLEAN state → autodeploy → real workflow OUTPUT.

The single test that proves the FULL chain works from a clean state, exercising the autodeploy the
per-component live tests each cover only a SLICE of:
  * Leg 1 (rhea): TRUNCATE the tool catalog, then let the orchestrator autodeploy re-provision it
    (``ensure_catalog_seeded`` — container up + embedding-model auto-pull + ingest) and align a real
    3-sequence FASTA through the thin-HTTP-client → rhea-server → MUSCLE chain.
  * Leg 2 (PyMOL): REMOVE the ``apecx-pymol`` image, then let ``StructuralReasoningStep.process``
    rebuild it (via ``find_and_establish_tool`` → ``ensure_image``) and compute real SASA.

Both legs assert on the OUTPUT VALUE (G127 honesty — a real aligned FASTA / real SASA residue
numbers), never on run ``status``. Both are DESTRUCTIVE-but-self-healing (they re-seed / rebuild what
they cleared) and ``pytest.mark.integration`` (never in the default ``make unit`` run).

Complements — does NOT duplicate — the per-component tests: ``test_catalog_ingestion_live.py``
(catalog only), nanobrain ``test_rhea_synthesize_muscle_run_live.py`` (assumes rhea already up +
seeded), ``test_structural_reasoning_pymol.py`` (SASA gated on the image ALREADY being present, so it
never exercises the build-from-absent path this leg drives end to end).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import urllib.request

import pytest

pytestmark = pytest.mark.integration

_PG_CONTAINER = "apecx-rhea-postgres"
_PYMOL_IMAGE = "apecx-pymol:3.1.0"


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_DOCKER = shutil.which("docker")
_PG_UP = _port_open("localhost", 5435)
_RHEA_UP = _port_open("localhost", 3001)
_OLLAMA_UP = _port_open("localhost", 11434)

# --- Leg 1 (rhea) gate: docker + the whole live stack the autodeploy + workflow need. ---
_skip_rhea = pytest.mark.skipif(
    not (_DOCKER and _PG_UP and _RHEA_UP and _OLLAMA_UP),
    reason=(
        "rhea leg needs the live stack: docker + postgres :5435 + rhea-server :3001 + Ollama "
        ":11434 (embedding backend for the ingest). Start via `apecx-mcp` / the orchestrator."
    ),
)

# --- Leg 2 (PyMOL) gate: docker only (the image build IS what we exercise); RCSB checked at runtime. ---
_skip_pymol = pytest.mark.skipif(
    not _DOCKER, reason="PyMOL leg needs docker (it rebuilds the image)"
)


def _catalog_rows() -> int:
    """Row count of galaxytools via psql in the postgres container (-1 on a query error)."""
    res = subprocess.run(
        [
            _DOCKER,
            "exec",
            _PG_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "rhea",
            "-tAc",
            "SELECT COUNT(*) FROM galaxytools;",
        ],
        capture_output=True,
        timeout=15,
    )
    try:
        return int(res.stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return -1


def _parse_fasta(text: str) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    header: str | None = None
    seq: list[str] = []
    for line in text.splitlines():
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(seq)))
            header, seq = line[1:].strip(), []
        elif line.strip():
            seq.append(line.strip())
    if header is not None:
        records.append((header, "".join(seq)))
    return records


# Three short globin N-termini — a real MSA the worker aligns in seconds.
_FASTA = (
    ">seqA\nMVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF\n"
    ">seqB\nMVLSAADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF\n"
    ">seqC\nMVLSGEDKSNIKAAWGKIGGHGAEYGAEALERMFASFPTTKTYFPHF\n"
)


@_skip_rhea
def test_e2e_rhea_leg_clean_catalog_autodeploys_then_aligns():
    """CLEAN catalog → orchestrator autodeploy → real MUSCLE alignment (nothing pre-seeded).

    The chain: TRUNCATE galaxytools (row count 0) → ``InfraOrchestrator.ensure_catalog_seeded`` (the
    apecx-mcp-startup autodeploy path: rhea container + embedding-model pull + catalog ingest) →
    drive the muscle workflow (RheaFileToolStep thin HTTP client → rhea-server → MUSCLE). Success is
    read from the aligned-FASTA VALUE (equal-length records = real MSA columns), never run status.
    """
    from nanobrain.library.tools.rhea_step_synthesizer import synthesize_rhea_step
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    from apecx_integration.infrastructure.orchestrator import (
        InfraOrchestrator,
        reset_orchestrator_for_testing,
    )

    reset_orchestrator_for_testing()

    # CLEAN STATE: empty the catalog so the autodeploy MUST re-provision it.
    subprocess.run(
        [
            _DOCKER,
            "exec",
            _PG_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "rhea",
            "-c",
            "TRUNCATE galaxytools;",
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )
    assert _catalog_rows() == 0, "TRUNCATE did not empty the catalog"

    async def _drive():
        # 1) Autodeploy: rhea container up + embedding-model auto-pull + catalog ingest.
        seeded = await InfraOrchestrator().ensure_catalog_seeded(timeout_s=300)
        assert seeded["seeded"] is True, f"autodeploy did not seed the catalog: {seeded!r}"
        # 2) Drive the muscle workflow the same way `run_workflow` drives a rhea catalog workflow.
        spec = await synthesize_rhea_step(
            "muscle",
            mcp_url="http://localhost:3001/mcp/",
            find_tools_query="muscle multiple sequence alignment",
            static_tool_args={"diags": False},
        )
        builder = WorkflowBuilder("e2e_muscle", "zero-config e2e muscle")
        builder.add_input("wf_in", "DataUnitMemory")
        builder.add_output("wf_out", "DataUnitMemory")
        builder.add_rhea_step(
            "tool",
            spec,
            input_data_units={
                "tool_in": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "tool_in"}
            },
            output_data_units={
                "output_files": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "output_files",
                }
            },
            triggers=[
                {"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "tool_in"}
            ],
        )
        builder.add_link("wf_in", "tool.tool_in", link_type="direct")
        builder.add_link("tool.output_files", "wf_out", link_type="direct")
        wf = builder.load()
        return await wf.run(
            {"wf_in": {"fasta_name": "seqs.fasta", "fasta_text": _FASTA}},
            timeout=900.0,
            settle_ms=1000,
            raise_on_cascade_timeout=False,
        )

    out = asyncio.run(_drive())

    # The autodeploy actually re-seeded the catalog (VALUE, not status).
    assert _catalog_rows() > 0, "catalog still empty after autodeploy"
    # G127: read success from the OUTPUT VALUE.
    wf_out = out.get("wf_out")
    assert isinstance(wf_out, dict) and wf_out, (
        f"muscle produced no output files (status={out.get('status')}) — G127 silent failure"
    )
    align_key = next((k for k in wf_out if "align" in k.lower() and "html" not in k.lower()), None)
    assert align_key is not None, f"no alignment FASTA in outputs: {list(wf_out)}"
    records = _parse_fasta(wf_out[align_key])
    assert len(records) >= 1, f"alignment had no records: {wf_out[align_key]!r}"
    lengths = {len(seq) for _, seq in records}
    assert len(lengths) == 1, f"aligned records not equal length (not a real MSA): {lengths}"
    assert lengths.pop() > 0, "aligned records are empty"
    assert {h for h, _ in records} >= {"seqA", "seqB", "seqC"}, "input sequences did not survive"

    reset_orchestrator_for_testing()


# 3N40 = CHIKV E1/E2 glycoprotein; a real contiguous chain-F segment (maps deterministically).
_PDB_RECORD = {"subject": "pdb:3N40", "structural_source": "pdb"}
_REAL_REGION = {"start": 33, "end": 46, "length": 14, "consensus": "MVLEMELLSVTLEP"}


def _image_present() -> bool:
    if _DOCKER is None:
        return False
    return (
        subprocess.run(
            [_DOCKER, "image", "inspect", _PYMOL_IMAGE], capture_output=True, timeout=20
        ).returncode
        == 0
    )


def _require_rcsb(pdb_id: str = "3N40") -> None:
    try:
        with urllib.request.urlopen(
            f"https://files.rcsb.org/download/{pdb_id}.cif", timeout=15
        ) as r:
            if r.status != 200:
                pytest.skip("RCSB returned non-200")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"RCSB not reachable: {exc}")


@_skip_pymol
def test_e2e_pymol_leg_removed_image_autobuilds_then_computes_sasa():
    """REMOVE the pymol image → StructuralReasoningStep rebuilds it → real SASA residue numbers.

    Drives the CLEAN-state PyMOL autodeploy + real output in ONE flow: `docker rmi` the image, then
    ``StructuralReasoningStep.process`` (whose ``find_and_establish_tool('pymol:pymol_sasa')`` seam
    auto-builds the image on first use, ~5 min) returns real solvent-exposed residue NUMBERS from
    real PyMOL SASA. The existing SASA test SKIPS when the image is absent — this one BUILDS it.
    G127: assert on the SASA residue VALUES, never on status.
    """
    _require_rcsb()
    import tempfile
    from pathlib import Path

    from apecx_integration.composition.steps.structural_reasoning_step import (
        StructuralReasoningStep,
    )

    # CLEAN STATE: remove the image so the run MUST auto-build it.
    subprocess.run([_DOCKER, "rmi", "-f", _PYMOL_IMAGE], capture_output=True, timeout=60)
    assert not _image_present(), "pymol image still present after `docker rmi`"

    cfg = Path(tempfile.mkdtemp(prefix="apecx_e2e_pymol_")) / "reasoning.yml"
    cfg.write_text("name: e2e_reasoning\nrsa_threshold: 0.25\nmin_map_identity: 0.7\n")
    step = StructuralReasoningStep.from_config(str(cfg))
    bundle = {
        "query": "chikungunya E1 glycoprotein conserved epitopes",
        "conserved_regions": [dict(_REAL_REGION)],
        "structural_records": [dict(_PDB_RECORD)],
    }
    out = asyncio.run(step.process(bundle))
    sr = out["structural_reasoning"]

    # The run auto-built the image (VALUE: it's present again) and produced real SASA.
    assert _image_present(), "the run did not auto-build the pymol image"
    assert sr["available"] is True, f"structural leg degraded: {sr.get('note')}"
    assert sr["pdb_id"] == "3N40"
    assert sr["n_exposed"] + sr["n_buried"] == sr["n_mapped_residues"]
    assert sr["n_exposed"] >= 1, "no real solvent-exposed residues from PyMOL SASA"
    exposed_resis = [e["resi"] for e in sr["exposed_residues"]]
    assert all(isinstance(r, int) for r in exposed_resis), "SASA residue numbers are not ints"
    for e in sr["exposed_residues"]:
        assert e["rsa"] >= 0.25 and e["sasa"] > 0.0, f"exposed residue has no real SASA: {e!r}"
