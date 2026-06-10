"""NCBI Taxonomy dump downloader.

Downloads ``taxdump.tar.gz`` from the NCBI FTP mirror and extracts the
four dump files the synonym dictionary build consumes:

- ``nodes.dmp``    — parent-child taxonomy hierarchy (rank, parent_id).
- ``merged.dmp``   — old-id → new-id redirects (taxon merges).
- ``names.dmp``    — all 7 NCBI name classes (scientific name, synonym,
                     equivalent name, common name, genbank common name,
                     acronym, blast name). **Added 2026-06-08 (SC-A2).**
                     Before this change, synonyms were obtained only via
                     per-IRI OLS lookups during build, bounded by the
                     corpus's resolved IRI set.
- ``delnodes.dmp`` — deleted taxon ids. Used by lookup to surface a loud
                     ``"taxon deleted"`` unresolved-status rather than a
                     silent miss when a user pastes an obsolete IRI.
                     **Added 2026-06-08 (SC-A2).**

All other members of the archive are discarded.

Disk-space note: ``names.dmp`` is ~250 MB unpacked (full NCBI Taxonomy).
The virus-subtree filter (SC-A3/A4) keeps the SQLite artifact small;
the on-disk taxdump cache still carries the full file.

This module is wrapped by
:class:`apecx_integration.synonym_dictionary.workflow.taxdump_fetch_step.TaxdumpFetchStep`.
End users do not invoke it directly — the fetch runs as the first step
of the nanobrain ``dictionary_build_workflow``, triggered lazily at
apecx-mcp startup (see ``synonym_dictionary.workflow.bootstrap.ensure_dictionary``).
"""

from __future__ import annotations

import logging
import tarfile
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TAXDUMP_URL = "https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz"
# The set of dump files we extract from taxdump.tar.gz.  Adding a member
# here is a backwards-incompatible cache-format change — any existing
# cache directory missing the new file triggers a re-extract (see the
# ``all four files present`` check below).
_WANTED = frozenset({"nodes.dmp", "merged.dmp", "names.dmp", "delnodes.dmp"})
_CHUNK = 1024 * 1024  # 1 MiB streaming chunks


def fetch_taxdump(
    dest_dir: Path | str,
    *,
    url: str = TAXDUMP_URL,
    force: bool = False,
    show_progress: bool = False,
) -> tuple[Path, Path, Path, Path]:
    """Download NCBI taxdump and extract the four required dump files.

    Parameters
    ----------
    dest_dir:
        Directory where the extracted files will be written.  Created if
        it does not exist.
    url:
        Override the download URL (useful for mirrors or tests).
    force:
        Re-download and re-extract even if the output files already exist.
    show_progress:
        Print a progress bar to stderr during download.

    Returns
    -------
    (nodes_dmp_path, merged_dmp_path, names_dmp_path, delnodes_dmp_path)
        All four paths are guaranteed to exist when this function returns.
        The order is positional and is consumed by
        :class:`apecx_integration.synonym_dictionary.workflow.taxdump_fetch_step.TaxdumpFetchStep`;
        do not reorder without updating that caller.

    Raises
    ------
    httpx.HTTPError
        If the download fails.
    KeyError
        If the archive does not contain all four required dump files.
    """
    dest_dir = Path(dest_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    nodes_path = dest_dir / "nodes.dmp"
    merged_path = dest_dir / "merged.dmp"
    names_path = dest_dir / "names.dmp"
    delnodes_path = dest_dir / "delnodes.dmp"

    # All four files must be present for the cache to count as complete.
    # An existing 2-file cache (pre-SC-A2) fails this check, triggering a
    # re-extract from the cached tarball (cheap — no re-download needed).
    if not force and all(p.exists() for p in (nodes_path, merged_path, names_path, delnodes_path)):
        log.info("taxdump already present at %s — skipping download", dest_dir)
        return nodes_path, merged_path, names_path, delnodes_path

    archive_path = dest_dir / "taxdump.tar.gz"

    if force or not archive_path.exists():
        _download(url, archive_path, show_progress=show_progress)
    else:
        log.info("archive already cached at %s — skipping download", archive_path)

    _extract(archive_path, dest_dir)
    return nodes_path, merged_path, names_path, delnodes_path


def _download(url: str, dest: Path, *, show_progress: bool) -> None:
    log.info("Downloading taxdump from %s → %s", url, dest)
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=_CHUNK):
                fh.write(chunk)
                downloaded += len(chunk)
                if show_progress and total:
                    pct = downloaded * 100 // total
                    print(f"\r  {pct:3d}% ({downloaded // (1024 * 1024)} MiB)", end="", flush=True)
        if show_progress:
            print()  # newline after progress bar
    log.info("download complete: %d bytes", dest.stat().st_size)


def _extract(archive: Path, dest_dir: Path) -> None:
    log.info("Extracting %s from %s", sorted(_WANTED), archive)
    missing = set(_WANTED)
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            name = Path(member.name).name  # strip leading path components
            if name not in _WANTED:
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            (dest_dir / name).write_bytes(src.read())
            missing.discard(name)
            log.debug("extracted %s", name)
    if missing:
        raise KeyError(
            f"taxdump archive at {archive} is missing: {sorted(missing)}.  "
            "This is unexpected — the NCBI archive always ships these four files."
        )
    log.info("extraction complete: %s written to %s", sorted(_WANTED), dest_dir)
