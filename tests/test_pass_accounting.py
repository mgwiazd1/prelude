"""Regression tests for pass #1 accounting bugs (v1.6).

Bugs being locked down:
- B1: per-row timestamps made the first snapshot undiffable → one pass-level ts.
- B2: a failed (non-200) wallet must never be stored (no row, no empty marker).
- B3: a 200 with zero rows must be stored as a verified-empty marker so the
      partition (stored-with-rows / stored-empty / dead-letter) is DB-recoverable.
- B4: roster addresses must be stripped + deduped (trailing-space addresses
      cause Nansen 500 'Invalid parameter').
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3  # noqa: E402

from prelude import poller  # noqa: E402


class FakeClient:
    """Deterministic stand-in for NansenClient — no network."""

    def __init__(self, outcomes):
        # outcomes: {address: ("ok", [rows]) | ("fail", "error")}
        self.outcomes = outcomes
        self.credits_remaining = 100000

    def current_balance(self, address, chain):
        import httpx
        kind, payload = self.outcomes[address]
        if kind == "fail":
            req = httpx.Request("POST", "https://api.nansen.ai/test")
            resp = httpx.Response(500, request=req)
            raise httpx.HTTPStatusError(payload, request=req, response=resp)
        return {"data": payload, "meta": {}}


def _roster(tmp, names):
    import json
    path = os.path.join(tmp, "roster.json")
    json.dump({"roster": [{"address": f"ADDR{i:03d}", "chain": "solana",
                            "tier": "operator", "tier_weight": 1.0,
                            "name": n, "status": "active"}
                           for i, n in enumerate(names)]}, open(path, "w"))
    return path


def test_one_pass_one_snapshot_id(tmp_path):
    """B1: one pass = exactly one snapshot_id; every row carries it. ts is display-only."""
    names = ["A", "B", "C"]
    rpath = _roster(str(tmp_path), names)
    outcomes = {f"ADDR{i:03d}": ("ok", [
        {"token_address": "T1", "token_symbol": "SOL", "value_usd": 10000.0,
         "price_usd": 100.0, "token_amount": 100.0},
        {"token_address": "T2", "token_symbol": "USDC", "value_usd": 5000.0,
         "price_usd": 1.0, "token_amount": 5000.0}])
        for i in range(3)}
    conn = _fresh_conn(tmp_path)
    poller.snapshot_all(client=FakeClient(outcomes), conn=conn,
                        roster=poller.load_roster(rpath))
    sids = [row[0] for row in conn.execute(
        "SELECT DISTINCT snapshot_id FROM balance_snapshots")]
    assert len(sids) == 1, f"pass wrote {len(sids)} snapshot_ids: {sids}"
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0] == 6
    # two back-to-back passes must get distinct snapshot_ids (overlap-proof)
    poller.snapshot_all(client=FakeClient(outcomes), conn=conn,
                        roster=poller.load_roster(rpath))
    sids2 = [row[0] for row in conn.execute(
        "SELECT DISTINCT snapshot_id FROM balance_snapshots")]
    assert len(sids2) == 2, f"two passes shared a snapshot_id: {sids2}"


def _fresh_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.executescript(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations", "001_init.sql")).read())
    return conn


def test_failed_wallet_writes_nothing(tmp_path):
    """B2: a non-200 wallet lands in the dead-letter and in NO table row."""
    names = ["A", "B"]
    rpath = _roster(str(tmp_path), names)
    outcomes = {
        "ADDR000": ("ok", [{"token_address": "T1", "token_symbol": "SOL",
                             "value_usd": 10000.0, "price_usd": 100.0,
                             "token_amount": 100.0}]),
        "ADDR001": ("fail", "500 Invalid parameter"),
    }
    conn = _fresh_conn(tmp_path)
    poller.snapshot_all(client=FakeClient(outcomes), conn=conn,
                        roster=poller.load_roster(rpath))
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots WHERE address='ADDR001'").fetchone()[0] == 0, \
        "failed wallet must not be stored — not even as an empty marker"
    assert conn.execute("SELECT COUNT(*) FROM balance_snapshots WHERE address='ADDR000'").fetchone()[0] == 1


def test_empty_wallet_verified_marker(tmp_path):
    """B3: a 200 with zero rows is stored as a __empty__ marker (verifiable-empty)."""
    names = ["A"]
    rpath = _roster(str(tmp_path), names)
    outcomes = {"ADDR000": ("ok", [])}
    conn = _fresh_conn(tmp_path)
    poller.snapshot_all(client=FakeClient(outcomes), conn=conn,
                        roster=poller.load_roster(rpath))
    row = conn.execute("SELECT token, value_usd FROM balance_snapshots WHERE address='ADDR000'").fetchone()
    assert row == ("__empty__", 0.0)


def test_roster_strip_and_dedupe(tmp_path):
    """B4: trailing-space addresses are stripped; dupes (same stripped addr) collapse."""
    import json
    path = str(tmp_path / "r.json")
    json.dump({"roster": [
        {"address": "ABC123   ", "chain": "solana", "tier": "operator", "tier_weight": 1.0,
         "name": "dup1", "status": "active"},
        {"address": "ABC123", "chain": "solana", "tier": "operator", "tier_weight": 1.0,
         "name": "dup2", "status": "active"},
        {"address": "DEF456", "chain": "solana", "tier": "operator", "tier_weight": 1.0,
         "name": "ok", "status": "active"},
    ]}, open(path, "w"))
    roster = poller.load_roster(path)
    assert len(roster) == 2, f"expected 2 after dedupe, got {len(roster)}"
    assert all(w["address"] == w["address"].strip() for w in roster)
    assert {w["address"] for w in roster} == {"ABC123", "DEF456"}


def test_real_roster_no_bad_addresses():
    """The committed private roster must contain no unstrippable/duplicate addresses."""
    roster = poller.load_roster()
    addrs = [w["address"] for w in roster if w.get("status", "active") == "active"]
    assert all(a == a.strip() and 0 < len(a) < 46 for a in addrs)
    assert len(addrs) == len(set(addrs)), "active roster has duplicate addresses"
