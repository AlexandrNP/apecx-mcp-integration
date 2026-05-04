"""NCBI Taxonomy dump downloader.

Downloads ``taxdump.tar.gz`` from the NCBI FTP mirror and extracts only
``nodes.dmp`` and ``merged.dmp`` to a local cache directory.  All other
members of the archive are discarded.

Usage (CLI):
    apecx-fetch-taxdump --output ~/.cache/apecx/taxdump

Usage (Python):
    from apecx_integration.synonym_dictionary.taxdump_fetcher import fetch_taxdump
    nodes_path, merged_path = fetch_taxdump(Path("~/.cache/apecx/taxdump"))

These paths can then be passed to ``apecx-build-dictionary`` via
``--ncbitaxon-nodes`` / ``--ncbitaxon-merged``.
"""

from __future__ import annotations

import logging
import tarfile
from pathlib import Path

import requests

log = logging.getLogger(__name__)

TAXDUMP_URL = "https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz"
_WANTED = frozenset({"nodes.dmp", "merged.dmp"})
_CHUNK = 1024 * 1024  # 1 MiB streaming chunks


def fetch_taxdump(
    dest_dir: Path | str,
    *,
    url: str = TAXDUMP_URL,
    force: bool = False,
    show_progress: bool = False,
) -> tuple[Path, Path]:
    """Download NCBI taxdump and extract nodes.dmp + merged.dmp.

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
    (nodes_dmp_path, merged_dmp_path)
        Both paths are guaranteed to exist when this function returns.

    Raises
    ------
    requests.HTTPError
        If the download fails.
    KeyError
        If the archive does not contain nodes.dmp or merged.dmp.
    """
    dest_dir = Path(dest_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    nodes_path = dest_dir / "nodes.dmp"
    merged_path = dest_dir / "merged.dmp"

    if not force and nodes_path.exists() and merged_path.exists():
        log.info("taxdump already present at %s — skipping download", dest_dir)
        return nodes_path, merged_path

    archive_path = dest_dir / "taxdump.tar.gz"

    if force or not archive_path.exists():
        _download(url, archive_path, show_progress=show_progress)
    else:
        log.info("archive already cached at %s — skipping download", archive_path)

    _extract(archive_path, dest_dir)
    return nodes_path, merged_path


def _download(url: str, dest: Path, *, show_progress: bool) -> None:
    log.info("Downloading taxdump from %s → %s", url, dest)
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                fh.write(chunk)
                downloaded += len(chunk)
                if show_progress and total:
                    pct = downloaded * 100 // total
                    print(f"\r  {pct:3d}% ({downloaded // (1024*1024)} MiB)", end="", flush=True)
        if show_progress:
            print()  # newline after progress bar
    log.info("download complete: %d bytes", dest.stat().st_size)


def _extract(archive: Path, dest_dir: Path) -> None:
    log.info("Extracting nodes.dmp + merged.dmp from %s", archive)
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
            "This is unexpected — the NCBI archive always ships both files."
        )
    log.info("extraction complete: nodes.dmp + merged.dmp written to %s", dest_dir)
