"""StructuralReasoningStep — map sequence conservation onto 3D structure (E2-P).

The structural-LEVEL reasoning leg of ``viral_epitope_evidence_review``. It sits
AFTER ``merge`` (so it sees BOTH the MSA-derived conserved positions and the
PDB/EMDB structural records) and BEFORE ``review`` (so the synthesis can cite its
result). It is *real structural reasoning*, not retrieval: it picks a candidate PDB
structure from ``structural_records``, runs a CONTAINERIZED, headless, open-source
PyMOL job that

1. loads the (host-pre-fetched) structure — the BIOLOGICAL ASSEMBLY (functional
   oligomer, ``{pdb}.pdb1``) when one is deposited, else the asymmetric unit (named
   degrade), so accessibility is judged on the oligomer an antibody actually meets,
2. maps each conserved region's consensus motif onto the structure's chain residues
   (ungapped sliding-window identity — see ``_pymol_sasa.map_motif_to_chain``),
3. computes PER-RESIDUE SASA with PINNED settings (``dot_solvent=1``,
   ``dot_density=3``) and classifies each conserved residue EXPOSED vs BURIED
   (relative-SASA cutoff — epitopes are solvent-exposed, so the exposed conserved
   residues are the candidate epitope residues), and
4. computes a CA–CA contact map over the mapped residues.

The numeric SASA/mapping work runs inside the ``apecx-pymol`` container
(``docker run`` shell-out, network-isolated, host fetches the immutable RCSB
structure so the container needs no network). The structure download is cached and
RCSB PDB entries are immutable, the PyMOL version is pinned in the image and
recorded in the output, and the SASA settings are pinned — same structure + same
conserved positions → byte-stable exposed/buried classification.

RELIABILITY (G127): this step NEVER raises on a structural failure. No candidate
structure, Docker/image unavailable, fetch failure, container error, or a structure
onto which nothing maps — every case DEGRADES LOUD (a named note in both the bundle
and the stage report) and passes the bundle through, so ``merge → reasoning →
review`` always reaches synthesis with the rest of the evidence intact. It raises
ONLY on a broken wiring contract (non-dict input).

Output: the same bundle, plus ``bundle["structural_reasoning"]`` (machine-readable
result the synthesis can cite) and a ``structural_reasoning`` stage report (order 3)
rendered into the synthesis ``### Reasoning trace``.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents.globus_search._datacite import datacite_subjects, datacite_title
from apecx_integration.composition.steps import _pymol_sasa as sasa
from apecx_integration.composition.steps._stage_report import append_stage_report

log = logging.getLogger(__name__)

_INPUT_KEY = "reasoning_input"
_STAGE = "structural_reasoning"
_STAGE_ORDER = 3
# The pure mapping/SASA helpers live in the package (host-importable); the headless
# PyMOL job script is a CONTAINER artifact that lives with its Dockerfile under
# ``docker/pymol/`` (it imports ``pymol2`` + ``_pymol_sasa``, neither resolvable in
# the host venv). Both are copied into the per-run workdir mounted into the container.
_SASA_HELPER = Path(__file__).resolve().parent / "_pymol_sasa.py"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_JOB_SCRIPT = _REPO_ROOT / "docker" / "pymol" / "_pymol_job.py"
# Host-side cache for immutable RCSB structure files (keyed by PDB id).
_STRUCTURE_CACHE = Path(
    os.environ.get("APECX_PYMOL_STRUCTURE_CACHE", str(Path.home() / ".cache" / "apecx_pymol"))
)
_RCSB_CIF_URL = "https://files.rcsb.org/download/{pdb_id}.cif"
# Biological assembly 1 (the functional oligomer) in gzipped legacy-PDB format. SASA
# must be computed over THIS, not the AU .cif — an interface residue reads exposed in
# the AU but is buried once the assembly's symmetry copies are present. Falls back to
# the AU .cif on 404 (no biological assembly deposited).
_RCSB_ASSEMBLY_URL = "https://files.rcsb.org/download/{pdb_id}.pdb1.gz"
_KIND_ASSEMBLY = "assembly_1"
_KIND_AU = "asymmetric_unit"

# Structure-relevance ranking (P1). Epitopes sit on SURFACE ANTIGENS, so when the
# structural corpus returns several records for a virus we must NOT blindly take the
# first by search rank — on CHIKV that picked 2CXD (capsid protease), an internal
# protein, instead of the E1/E2 envelope glycoprotein an epitope map needs. We score
# each record's DataCite title+subjects: (a) the query's ``protein`` term(s) dominate,
# (b) surface-antigen vocabulary boosts, (c) internal-protein vocabulary penalizes.
# Ties keep the upstream search rank (so a no-signal corpus still falls back to "first
# loadable").
_PROTEIN_WEIGHT = 5.0
_SURFACE_WEIGHT = 2.0
_INTERNAL_WEIGHT = 2.0
# Generic tokens that, used as a protein term, would match almost every structural
# title and so carry no discriminating signal.
_PROTEIN_STOPWORDS = frozenset({"protein", "the", "and", "of"})
_SURFACE_KEYWORDS = (
    "envelope",
    "glycoprotein",
    "spike",
    "hemagglutinin",
    "E1",
    "E2",
    "E3",
    "E protein",
    "fusion",
    "surface",
    "neutralizing",
    "Fab",
    "antibody",
)
_INTERNAL_KEYWORDS = (
    "capsid",
    "protease",
    "nsP",
    "polymerase",
    "methyltransferase",
    "helicase",
    "nucleocapsid",
)
# Short / ambiguous keywords matched on word boundaries (substring matching would
# fire spuriously, e.g. "e2" inside "phase2"); the rest match as substrings.
_WORD_BOUNDARY_KEYWORDS = frozenset({"e1", "e2", "e3", "e protein", "fab", "surface"})


def _record_text(rec: dict[str, Any]) -> str:
    """Lower-cased DataCite title + subjects for a structural record (the relevance
    haystack). Excludes the bare record id, which carries no semantic signal."""
    content = rec.get("content") or {}
    title = datacite_title(content) or ""
    subjects = datacite_subjects(content, limit=20)
    return f"{title} {' '.join(subjects)}".lower()


def _kw_match(keyword: str, text: str) -> bool:
    kw = keyword.lower()
    if kw == "nsp":
        return re.search(r"\bns[ps]\d*\b", text) is not None
    if kw in _WORD_BOUNDARY_KEYWORDS:
        return re.search(r"\b" + re.escape(kw) + r"\b", text) is not None
    return kw in text


def _protein_terms(protein: Any) -> list[str]:
    if not isinstance(protein, str):
        return []
    toks = [t for t in re.split(r"[^a-z0-9]+", protein.lower()) if len(t) >= 2]
    return [t for t in toks if t not in _PROTEIN_STOPWORDS]


def _score_record(rec: dict[str, Any], protein_terms: list[str]) -> tuple[float, list[str]]:
    text = _record_text(rec)
    score = 0.0
    reasons: list[str] = []
    matched_protein = [t for t in protein_terms if re.search(r"\b" + re.escape(t) + r"\b", text)]
    if matched_protein:
        score += _PROTEIN_WEIGHT * len(matched_protein)
        reasons.append("matches query protein term(s): " + ", ".join(matched_protein))
    surf = [kw for kw in _SURFACE_KEYWORDS if _kw_match(kw, text)]
    if surf:
        score += _SURFACE_WEIGHT * len(surf)
        reasons.append("surface-antigen signal: " + ", ".join(surf))
    intern = [kw for kw in _INTERNAL_KEYWORDS if _kw_match(kw, text)]
    if intern:
        score -= _INTERNAL_WEIGHT * len(intern)
        reasons.append("internal-protein signal (deprioritized): " + ", ".join(intern))
    return score, reasons


def rank_structural_records(
    records: list[dict[str, Any]], protein: Any = None
) -> list[dict[str, Any]]:
    """Rank structural records for epitope relevance (highest first).

    Returns a list of ``{subject, pdb_id, score, reasons, title, _idx}`` sorted by
    descending score, ties broken by the original (search-rank) order — so a corpus
    with no surface/protein signal degrades to "first loadable by search rank". A
    ``pdb_id`` of ``None`` marks a non-loadable record (e.g. an EMDB density map).
    """
    terms = _protein_terms(protein)
    ranked: list[dict[str, Any]] = []
    for idx, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            continue
        score, reasons = _score_record(rec, terms)
        ranked.append(
            {
                "subject": rec.get("subject"),
                "pdb_id": sasa.extract_pdb_id(rec),
                "score": score,
                "reasons": reasons,
                "title": datacite_title(rec.get("content") or {}),
                "_idx": idx,
            }
        )
    ranked.sort(key=lambda e: (-e["score"], e["_idx"]))
    return ranked


class StructuralReasoningStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    image: str = Field(
        default="apecx-pymol:3.1.0",
        description="Containerized open-source PyMOL image tag (version-pinned).",
    )
    rsa_threshold: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Relative-SASA cutoff: a conserved residue is EXPOSED when RSA >= this.",
    )
    min_map_identity: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum motif↔chain identity for a conserved region to map onto the structure.",
    )
    contact_cutoff: float = Field(
        default=8.0, gt=0.0, description="CA–CA distance (Å) defining a residue contact."
    )
    timeout_seconds: float = Field(
        default=300.0, gt=0.0, description="Wall-clock budget for the containerized PyMOL job."
    )
    memory_mb: int = Field(default=2048, gt=0, description="Container memory cap (MB).")

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class StructuralReasoningStep(BaseStep):
    COMPONENT_TYPE: str = "structural_reasoning_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return StructuralReasoningStepConfig

    @classmethod
    def extract_component_config(cls, config: StructuralReasoningStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "image": getattr(config, "image", "apecx-pymol:3.1.0"),
            "rsa_threshold": getattr(config, "rsa_threshold", 0.25),
            "min_map_identity": getattr(config, "min_map_identity", 0.7),
            "contact_cutoff": getattr(config, "contact_cutoff", 8.0),
            "timeout_seconds": getattr(config, "timeout_seconds", 300.0),
            "memory_mb": getattr(config, "memory_mb", 2048),
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._image: str = str(component_config.get("image", "apecx-pymol:3.1.0"))
        self._rsa_threshold: float = float(component_config.get("rsa_threshold", 0.25))
        self._min_map_identity: float = float(component_config.get("min_map_identity", 0.7))
        self._contact_cutoff: float = float(component_config.get("contact_cutoff", 8.0))
        self._timeout: float = float(component_config.get("timeout_seconds", 300.0))
        self._memory_mb: int = int(component_config.get("memory_mb", 2048))

    # ------------------------------------------------------------------ process
    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"StructuralReasoningStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        bundle = dict(input_data)
        regions = bundle.get("conserved_regions") or []
        records = bundle.get("structural_records") or []
        protein = bundle.get("protein")

        result, note = await self._reason(regions, records, protein)
        bundle["structural_reasoning"] = result

        markdown = self._render_markdown(result, note)
        append_stage_report(
            bundle,
            stage=_STAGE,
            order=_STAGE_ORDER,
            markdown=markdown,
            data={
                "available": bool(result.get("available")),
                "pdb_id": result.get("pdb_id"),
                "structure_kind": result.get("structure_kind"),
                "assembly_id": result.get("assembly_id"),
                "n_assembly_copies": result.get("n_assembly_copies"),
                "n_exposed": result.get("n_exposed"),
                "n_buried": result.get("n_buried"),
                "selection": result.get("selection"),
                "note": note,
            },
        )
        log.info(
            "StructuralReasoningStep %s: available=%s pdb=%s exposed=%s buried=%s",
            self.name,
            result.get("available"),
            result.get("pdb_id"),
            result.get("n_exposed"),
            result.get("n_buried"),
        )
        return bundle

    async def _reason(
        self,
        regions: list[dict[str, Any]],
        records: list[dict[str, Any]],
        protein: Any = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Run the structural reasoning, returning ``(result_dict, degrade_note)``.

        ``result_dict`` always carries ``available: bool`` and a ``note`` on degrade.
        Never raises — every failure mode returns a LOUD named note. Records are
        RELEVANCE-RANKED (P1) before selection: the best-ranked LOADABLE structure
        wins, so epitope mapping runs on a surface antigen rather than the first
        record by raw search rank.
        """
        ranked = rank_structural_records(records, protein)
        chosen = next((e for e in ranked if e.get("pdb_id")), None)
        ranking_summary = [
            {k: e[k] for k in ("subject", "pdb_id", "score", "reasons", "title")}
            for e in ranked[:5]
        ]
        if chosen is None:
            note = (
                f"No loadable PDB structure among {len(records)} structural record(s); "
                "structural-level reasoning skipped (EMDB density maps are not loadable as "
                "atomic coordinates)."
            )
            return {"available": False, "note": note, "ranking": ranking_summary}, note
        pdb_id = chosen["pdb_id"]
        selection = {
            "pdb_id": pdb_id,
            "score": chosen["score"],
            "reasons": chosen["reasons"],
            "title": chosen["title"],
            "considered": len(ranked),
        }
        if not regions:
            note = (
                f"No conserved regions were available to map onto structure {pdb_id}; "
                "structural-level reasoning skipped."
            )
            return {
                "available": False,
                "pdb_id": pdb_id,
                "selection": selection,
                "ranking": ranking_summary,
                "note": note,
            }, note

        if not _docker_available(self._image):
            note = (
                f"Containerized PyMOL image {self._image!r} is not available "
                "(docker missing or image not built); structural-level reasoning skipped. "
                "Build it with the repo's PyMOL Dockerfile to enable this stage."
            )
            return {
                "available": False,
                "pdb_id": pdb_id,
                "selection": selection,
                "ranking": ranking_summary,
                "note": note,
            }, note

        try:
            raw = await self._run_container(pdb_id, regions)
        except Exception as exc:  # noqa: BLE001 — degrade LOUD, never strand the workflow
            note = (
                f"Containerized PyMOL structural reasoning failed for {pdb_id} "
                f"({type(exc).__name__}: {exc}); other evidence still synthesized."
            )
            log.warning("StructuralReasoningStep %s: %s", self.name, note)
            return {
                "available": False,
                "pdb_id": pdb_id,
                "selection": selection,
                "ranking": ranking_summary,
                "note": note,
            }, note

        if not raw.get("ok"):
            note = raw.get("note") or f"PyMOL job returned no usable result for {pdb_id}."
            return {
                "available": False,
                "pdb_id": pdb_id,
                "selection": selection,
                "ranking": ranking_summary,
                "note": note,
            }, note

        result = {"available": True, "selection": selection, "ranking": ranking_summary, **raw}
        caveats: list[str] = []
        if raw.get("structure_kind") == _KIND_AU:
            # E3-1.3: AU fallback is always NAMED, never silent (CC-2 degrade-loud).
            caveats.append(
                f"accessibility computed over the asymmetric unit; no biological assembly "
                f"available in legacy PDB (pdb1) format for {pdb_id}"
            )
            result["assembly_caveat"] = caveats[-1]
        if not raw.get("n_mapped_regions"):
            caveats.append(
                raw.get("notes")
                and "; ".join(raw["notes"])
                or (f"No conserved region mapped onto chain {raw.get('chain')} of {pdb_id}.")
            )
        note = "; ".join(c for c in caveats if c) or None
        if note:
            result["note"] = note
        return result, note

    async def _run_container(self, pdb_id: str, regions: list[dict[str, Any]]) -> dict[str, Any]:
        """Fetch the biological assembly (host), then run the headless PyMOL job."""
        structure_path, kind = await asyncio.to_thread(_fetch_structure, pdb_id)
        return await self._run_pymol_on_file(pdb_id, structure_path, kind, regions)

    async def _run_pymol_on_file(
        self, pdb_id: str, structure_path: Path, kind: str, regions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run the headless PyMOL job in a network-isolated container on a host-fetched
        structure file. ``kind`` (``'assembly_1'`` | ``'asymmetric_unit'``) selects the
        load format and is recorded in the result. Split from ``_run_container`` so the
        AU-vs-assembly SASA comparison test can drive the same job on both files."""
        ext = "pdb1" if kind == _KIND_ASSEMBLY else "cif"
        with tempfile.TemporaryDirectory(prefix="apecx_pymol_") as tmp:
            workdir = Path(tmp)
            shutil.copy2(_JOB_SCRIPT, workdir / "_pymol_job.py")
            shutil.copy2(_SASA_HELPER, workdir / "_pymol_sasa.py")
            shutil.copy2(structure_path, workdir / f"{pdb_id}.{ext}")
            job = {
                "structure_path": f"/work/{pdb_id}.{ext}",
                "structure_kind": kind,
                "pdb_id": pdb_id,
                "conserved_regions": regions,
                "rsa_threshold": self._rsa_threshold,
                "min_map_identity": self._min_map_identity,
                "contact_cutoff": self._contact_cutoff,
            }
            (workdir / "job.json").write_text(json.dumps(job))

            argv = self._docker_argv(workdir)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            except TimeoutError as exc:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(
                    f"PyMOL container exceeded {self._timeout:.0f}s timeout"
                ) from exc

            result_path = workdir / "result.json"
            if proc.returncode != 0 or not result_path.exists():
                stderr = stderr_b.decode("utf-8", errors="replace")[-2000:]
                raise RuntimeError(
                    f"docker run exited {proc.returncode}; result.json "
                    f"{'present' if result_path.exists() else 'missing'}. stderr: {stderr}"
                )
            return json.loads(result_path.read_text())

    def _docker_argv(self, workdir: Path) -> list[str]:
        """Hardened ``docker run`` argv for the PyMOL job.

        Mirrors the hardening shape of ``composition/docker_sandbox.py``
        (``--network none``, ``--cap-drop ALL``, ``--security-opt
        no-new-privileges``, memory/pids caps) with two deliberate deviations: the
        ``/work`` mount is read-WRITE (the job writes ``result.json`` back) and the
        container runs as the HOST uid:gid so that result file is host-owned and
        writable. The structure is pre-fetched on the host, so the container is
        still fully network-isolated.
        """
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            f"{self._memory_mb}m",
            "--memory-swap",
            f"{self._memory_mb}m",
            "--pids-limit",
            "256",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,source={workdir.resolve()},target=/work",
            "--workdir",
            "/work",
            self._image,
            "python",
            "/work/_pymol_job.py",
            "/work/job.json",
            "/work/result.json",
        ]

    @staticmethod
    def _selection_prefix(result: dict[str, Any]) -> str:
        """Human-readable structure-selection rationale (P1 relevance ranking)."""
        sel = result.get("selection")
        if not isinstance(sel, dict):
            return ""
        why = "; ".join(sel.get("reasons") or []) or (
            "best-ranked loadable structure (no query-protein or surface-antigen keyword "
            "match — fell back to search rank)"
        )
        return (
            f"Selected PDB {sel.get('pdb_id')} from {sel.get('considered')} candidate(s): {why}. "
        )

    @staticmethod
    def _render_markdown(result: dict[str, Any], note: str | None) -> str:
        prefix = StructuralReasoningStep._selection_prefix(result)
        if not result.get("available"):
            return f"{prefix}Structural-level reasoning unavailable: {note}"
        pdb_id = result.get("pdb_id")
        chain = result.get("chain")
        n_mapped = result.get("n_mapped_residues", 0)
        n_exposed = result.get("n_exposed", 0)
        exposed = result.get("exposed_residues") or []
        resi_list = ", ".join(str(e.get("resi")) for e in exposed[:20])
        more = "" if len(exposed) <= 20 else f" (+{len(exposed) - 20} more)"
        pv = result.get("pymol_version")
        if result.get("structure_kind") == _KIND_ASSEMBLY:
            ctx = f"biological assembly 1, {result.get('n_assembly_copies')} copy(ies)"
        else:
            ctx = "asymmetric unit — no biological assembly deposited"
        tail = ""
        if result.get("assembly_caveat"):
            tail += f" Caveat: {result['assembly_caveat']}."
        if not result.get("n_mapped_regions"):
            tail += f" No conserved region mapped onto the structure ({note})."
        return (
            f"{prefix}Mapped conserved positions onto PDB {pdb_id} chain {chain} "
            f"({ctx}; PyMOL {pv}, dot_solvent=1/dot_density=3): {n_mapped} conserved "
            f"residue(s) mapped, {n_exposed} solvent-exposed (candidate epitope "
            f"residues): {resi_list}{more}.{tail}"
        )


# ----------------------------------------------------------------------- helpers


def _docker_available(image: str) -> bool:
    """True when the docker CLI is on PATH AND the pinned image is present locally."""
    if shutil.which("docker") is None:
        return False
    try:
        import subprocess

        out = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=15,
        )
        return out.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _fetch_au_cif(pdb_id: str) -> Path:
    """Download (and cache) the immutable RCSB asymmetric-unit ``.cif`` for ``pdb_id``."""
    _STRUCTURE_CACHE.mkdir(parents=True, exist_ok=True)
    dest = _STRUCTURE_CACHE / f"{pdb_id}.cif"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = _RCSB_CIF_URL.format(pdb_id=pdb_id)
    tmp = dest.with_suffix(".cif.part")
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 — fixed RCSB host
        tmp.write_bytes(resp.read())
    tmp.replace(dest)
    return dest


def _fetch_structure(pdb_id: str, *, prefer_assembly: bool = True) -> tuple[Path, str]:
    """Download (and cache) the immutable RCSB structure for ``pdb_id``.

    With ``prefer_assembly`` (default) fetch BIOLOGICAL ASSEMBLY 1 — the functional
    oligomer — from ``{pdb}.pdb1.gz`` (decompressed to ``{pdb}.pdb1`` in the cache) so
    SASA is computed over the oligomer, not the deposited asymmetric unit. On a 404
    (no assembly deposited) fall back to the AU ``.cif``. Returns ``(path, kind)`` where
    ``kind`` is ``'assembly_1'`` or ``'asymmetric_unit'`` (recorded in the result).
    Set ``prefer_assembly=False`` to force the AU (used to compare AU-vs-assembly SASA).
    Raises on a non-404 network/HTTP failure (the caller degrades LOUD)."""
    if not prefer_assembly:
        return _fetch_au_cif(pdb_id), _KIND_AU

    _STRUCTURE_CACHE.mkdir(parents=True, exist_ok=True)
    dest = _STRUCTURE_CACHE / f"{pdb_id}.pdb1"
    if dest.exists() and dest.stat().st_size > 0:
        return dest, _KIND_ASSEMBLY
    url = _RCSB_ASSEMBLY_URL.format(pdb_id=pdb_id)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 — fixed RCSB host
            raw = gzip.decompress(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # no biological assembly deposited → AU fallback (named upstream)
            return _fetch_au_cif(pdb_id), _KIND_AU
        raise
    tmp = dest.with_suffix(".pdb1.part")
    tmp.write_bytes(raw)
    tmp.replace(dest)
    return dest, _KIND_ASSEMBLY


__all__ = [
    "StructuralReasoningStep",
    "StructuralReasoningStepConfig",
    "rank_structural_records",
]
