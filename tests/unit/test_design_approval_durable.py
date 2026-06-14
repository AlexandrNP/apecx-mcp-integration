"""E4-1a — durable DesignApprovalStore: tokens survive a 'restart' (new store, same dir)."""

import json

from apecx_integration.composition.runtime.design_approval_store import DesignApprovalStore

_Q = "conserved chikungunya structural epitopes"
_P = "structural polyprotein"


def test_approved_token_survives_restart(tmp_path):
    """Request + approve in store A; a fresh store B on the SAME dir (a server restart) must
    still validate the token — the gap E4-1a closes."""
    a = DesignApprovalStore(persist_dir=tmp_path)
    token = a.request(query=_Q, protein=_P)
    a.approve(token)
    assert a.validate(token=token, query=_Q, protein=_P)[0]

    # "restart": a brand-new store instance over the same directory.
    b = DesignApprovalStore(persist_dir=tmp_path)
    ok, reason = b.validate(token=token, query=_Q, protein=_P)
    assert ok, reason  # approved status persisted across the restart
    # scope binding also survives — a different request is still rejected.
    assert not b.validate(token=token, query="dengue NS1", protein="NS1")[0]


def test_pending_then_approve_across_two_instances(tmp_path):
    """A token issued by one instance can be approved by another (the operator approves via a
    different process than the one that issued), and a third instance sees it approved."""
    issuer = DesignApprovalStore(persist_dir=tmp_path)
    token = issuer.request(query=_Q, protein=_P)

    approver = DesignApprovalStore(persist_dir=tmp_path)  # sees the pending token on load
    assert approver.get(token) is not None
    assert not approver.validate(token=token, query=_Q, protein=_P)[0]  # still pending
    approver.approve(token)

    reader = DesignApprovalStore(persist_dir=tmp_path)
    assert reader.validate(token=token, query=_Q, protein=_P)[0]


def test_in_memory_default_writes_no_files(tmp_path, monkeypatch):
    """Default (persist_dir=None) must NOT touch disk — keeps tests/usages pollution-free."""
    s = DesignApprovalStore()  # no persist_dir
    s.approve(s.request(query=_Q, protein=_P))
    # Nothing written anywhere we control; the store has no dir.
    assert s._dir is None


def test_corrupt_token_file_skipped_loud_not_crash(tmp_path, caplog):
    """A corrupt token file on load is skipped (degrade-loud), not a crash that loses the store."""
    good = DesignApprovalStore(persist_dir=tmp_path)
    token = good.request(query=_Q, protein=_P)
    good.approve(token)
    (tmp_path / "garbage.json").write_text("{not valid json", encoding="utf-8")

    reloaded = DesignApprovalStore(persist_dir=tmp_path)  # must not raise
    assert reloaded.validate(token=token, query=_Q, protein=_P)[0]  # the good token survived
    assert any("unreadable token file" in r.message for r in caplog.records)


def test_clear_removes_persisted_files(tmp_path):
    s = DesignApprovalStore(persist_dir=tmp_path)
    token = s.request(query=_Q, protein=_P)
    assert (tmp_path / f"{token}.json").is_file()
    s.clear()
    assert not (tmp_path / f"{token}.json").exists()
    # a fresh instance also sees nothing.
    assert DesignApprovalStore(persist_dir=tmp_path).get(token) is None


def test_persisted_file_shape(tmp_path):
    s = DesignApprovalStore(persist_dir=tmp_path)
    token = s.request(query=_Q, protein=_P)
    d = json.loads((tmp_path / f"{token}.json").read_text())
    assert d["token"] == token and d["status"] == "pending" and isinstance(d["scope"], list)


def test_fifo_bound_survives_restart(tmp_path):
    """The durable store is still FIFO-bounded; a restart that loads > max evicts oldest."""
    a = DesignApprovalStore(max_tokens=3, persist_dir=tmp_path)
    tokens = [a.request(query=f"q{i}", protein="p") for i in range(5)]
    # 5 issued, cap 3 → 2 oldest evicted (file + memory).
    assert not (tmp_path / f"{tokens[0]}.json").exists()
    b = DesignApprovalStore(max_tokens=3, persist_dir=tmp_path)
    assert b.get(tokens[-1]) is not None
    assert b.get(tokens[0]) is None
