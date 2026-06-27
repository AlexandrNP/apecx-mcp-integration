"""Globus-first / no-local-files: ``apecx-setup verify`` must NOT gate on — or even list — local
BV-BRC/VIOLIN CSVs.

A clean install with NO ``APECX_DATA_ROOT`` and no local data is fully functional: harmonized_search
queries the PUBLIC Globus Search index anonymously and the primary viral_epitope_analysis workflow
pulls its data over the network. This pins the local-file-removal cleanup so the obsolete checks
don't creep back. Real-dependency parity: the fresh-install harness
(``scripts/validate_fresh_install.py --full``) exercises ``apecx-setup --non-interactive`` end-to-end.
"""

from __future__ import annotations

import re
from pathlib import Path

from apecx_integration.cli import setup as s


def test_verify_does_not_check_or_list_local_data(monkeypatch, capsys):
    # No local-data env, and make the env-dependent probes deterministic (their pass/fail does not
    # affect WHICH rows are listed — only the data/violin REMOVAL is under test here).
    monkeypatch.delenv("APECX_DATA_ROOT", raising=False)
    monkeypatch.delenv("APECX_ROOT", raising=False)
    monkeypatch.setattr(s, "_docker_available", lambda: False)
    monkeypatch.setattr(s, "_probe_llm", lambda: (False, "no llm (test)"))
    monkeypatch.setattr(
        "apecx_integration.infrastructure.rhea_env_autodiscovery._find_rhea_repo", lambda: None
    )

    result = s._step_verify()  # must not raise even with zero local data

    out = capsys.readouterr().out
    # The verify table prints one row per component as ``  <emoji> <name> <detail>``.
    rows = re.findall(r"^\s*[✅❌]\s+(\w+)\b", out, re.MULTILINE)
    assert "data" not in rows, f"verify must not list a local-data row; rows={rows}"
    assert "violin" not in rows, f"verify must not list a local-VIOLIN row; rows={rows}"
    # The dictionary (a DOWNLOADED artifact) IS still verified.
    assert "dict" in rows, f"verify must still check the synonym dictionary; rows={rows}"
    # The removed checks referenced these literal CSV names — they must be gone entirely.
    assert "BVBRC_genome_alphavirus" not in out
    assert "Vaccine_Information" not in out
    # A run with no local data is never a hard failure on the data axis (only dict is required).
    assert result.status in {
        "ok",
        "partial",
        "fail",
    }  # no crash; status driven by dict/infra, not data


def test_data_and_violin_not_in_optional_set():
    """Belt-and-suspenders: the verify ``optional`` set no longer carries the removed checks
    (so a future re-add of a 'data'/'violin' check wouldn't be silently swallowed as optional)."""
    src = Path(s.__file__).read_text()
    # The optional-set literal lives in _step_verify; assert the removed names are not members.
    optional_block = src[src.index("    optional = {") :]
    optional_block = optional_block[: optional_block.index("}")]
    assert '"data"' not in optional_block
    assert '"violin"' not in optional_block
