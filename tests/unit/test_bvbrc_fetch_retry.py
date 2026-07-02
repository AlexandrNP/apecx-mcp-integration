"""DF2 — BvbrcProteinFastaStep._get_json retries transient BV-BRC network errors.

Real-data finding: BV-BRC (www.bv-brc.org) latency is high + VARIABLE (measured ~25s for a `*E1*` scan,
~160s for `*envelope*`, 15s for a 1-row count). A single 60s attempt with no retry intermittently
ReadTimeouts → the sequence-conservation + structural-analysis legs are SILENTLY starved (conserved
regions empty → structural_reasoning.available=False despite a valid PDB), while the report still says
"ok". `_get_json` now retries transient Timeout/ConnectionError with backoff; a deterministic HTTP error
is NOT retried. `requests.get` + `time.sleep` are mocked so the control flow is pinned with no network
and no real wait.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from apecx_integration.composition.steps import bvbrc_protein_fasta_step as mod
from apecx_integration.composition.steps.bvbrc_protein_fasta_step import BvbrcProteinFastaStep


def _stage(tmp_path: Path) -> BvbrcProteinFastaStep:
    p = tmp_path / "fasta.yml"
    p.write_text("name: fasta_test\n")  # defaults: timeout 90s, retries 2
    return BvbrcProteinFastaStep.from_config(str(p))


class _OkResp:
    def raise_for_status(self):
        pass

    def json(self):
        return [{"patric_id": "x"}]


def test_get_json_retries_transient_timeout_then_succeeds(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    calls = {"n": 0}

    def fake_get(url, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout("first attempt spikes past the timeout")
        return _OkResp()

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)  # no real backoff wait

    out = step._get_json("genome_feature", "eq(taxon_id,37124)")
    assert out == [{"patric_id": "x"}]
    assert calls["n"] == 2  # retried once after the ReadTimeout, then succeeded


def test_get_json_raises_loud_after_exhausting_retries(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    calls = {"n": 0}

    def always_timeout(url, timeout):
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("persistently down")

    monkeypatch.setattr(mod.requests, "get", always_timeout)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    with pytest.raises(requests.exceptions.ReadTimeout):
        step._get_json("genome_feature", "eq(taxon_id,37124)")
    assert calls["n"] == 3  # 1 initial + 2 retries (default request_retries=2), then surfaced loud


def test_get_json_does_not_retry_http_errors(tmp_path, monkeypatch):
    """A 4xx/5xx is deterministic — raise_for_status must fire once, NOT be retried."""
    step = _stage(tmp_path)
    calls = {"n": 0}

    class _ErrResp:
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("500 server error")

    def fake_get(url, timeout):
        calls["n"] += 1
        return _ErrResp()

    monkeypatch.setattr(mod.requests, "get", fake_get)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    with pytest.raises(requests.exceptions.HTTPError):
        step._get_json("genome_feature", "eq(taxon_id,37124)")
    assert calls["n"] == 1  # HTTP errors are not retried
