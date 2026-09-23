"""Fixture-only tests. No network, no key."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prelude import classifier, diff  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(HERE, "data", "fixtures")


def load_snapshots():
    return json.load(open(os.path.join(FIXTURES, "snapshots.json")))["snapshots"]


def make_event(kind="enter", prev=0.0, new=18500.0, t_lo="2026-09-20T06:00:00Z",
               t_hi="2026-09-21T06:00:00Z", token="0x42af...", chain="robinhood"):
    return {"address": "0xDE00000000000000000000000000000000000001", "chain": chain,
            "token": token, "symbol": "SCHIFFY", "kind": kind, "delta_usd": new - prev,
            "prev_usd": prev, "new_usd": new, "t_lo": t_lo, "t_hi": t_hi}


def test_diff_produces_enter():
    snaps = load_snapshots()
    events = diff.diff_snapshots(snaps[0], snaps[1])
    enters = [e for e in events if e["kind"] == "enter"]
    assert any(e["token"] == "0x42af..." for e in enters)


def test_diff_filters_small_positions():
    snaps = load_snapshots()
    events = diff.diff_snapshots(snaps[0], snaps[1])
    for e in events:
        assert max(e["prev_usd"], e["new_usd"]) >= 5_000


def test_steady_position_not_enter():
    # BNB wallet: 3000→3100 = $100, below both thresholds
    snaps = load_snapshots()
    events = diff.diff_snapshots(snaps[0], snaps[1])
    assert not any(e["token"].endswith("dead") for e in events)


def test_lead_confirmed_band():
    """v1.6: a 5h pre-onset lead is lead_confirmed (NOT stealth, NOT discarded)."""
    onsets = [{"slug": "x", "token": "0x42af...", "chain": "robinhood",
               "t_onset": "2026-09-21T18:00:00Z", "t_confirm": "2026-09-21T21:00:00Z",
               "z_narr": 2.8, "rank": 40, "moni_change": 0.5, "quiet_at_prev_snapshot": True}]
    # t_hi = 13:00 → onset 18:00 = 5h pre-onset, within 3–12h band
    e = make_event(t_hi="2026-09-21T13:00:00Z", t_lo="2026-09-21T10:00:00Z")
    c = classifier.classify(e, onsets=onsets, exchange_netflow_usd=None, divergence=None)
    assert c["cls"] == "lead_confirmed"
    assert abs(c["lead_hours"] - 5.0) < 1e-9
    # and a 20h lead is still stealth (alerting class)
    e2 = make_event(t_hi="2026-09-21T00:00:00Z", t_lo="2026-09-20T21:00:00Z")
    c2 = classifier.classify(e2, onsets=onsets, exchange_netflow_usd=None, divergence=None)
    assert c2["cls"] == "stealth_lead"
    # and a 2h lead (below the 3h resolvable floor) is concurrent, not a lead
    e3 = make_event(t_hi="2026-09-21T16:00:00Z", t_lo="2026-09-21T14:00:00Z")
    c3 = classifier.classify(e3, onsets=onsets, exchange_netflow_usd=None, divergence=None)
    assert c3["cls"] in ("concurrent", "chase")


def test_stealth_lead_classification():
    onsets = json.load(open(os.path.join(FIXTURES, "narrative.json")))["onsets"]
    e = make_event()  # t_hi 06:00, onset 18:00 → exactly the 12h stealth boundary
    c = classifier.classify(e, onsets=onsets, exchange_netflow_usd=None, divergence=None)
    assert c["cls"] == "stealth_lead"
    assert c["lead_hours"] == 12.0  # band upper = t_onset − t_hi


def test_chase_classification():
    onsets = json.load(open(os.path.join(FIXTURES, "narrative.json")))["onsets"]
    e = make_event(kind="add", prev=1000.0, new=3000.0,
                   t_lo="2026-09-22T01:00:00Z", t_hi="2026-09-22T06:00:00Z")
    c = classifier.classify(e, onsets=onsets, exchange_netflow_usd=None, divergence=None)
    assert c["cls"] == "chase"


def test_distribution_exchange_netflow():
    onsets = [{"slug": "x", "token": "0x42af...", "chain": "robinhood",
               "t_onset": "2026-09-21T12:00:00Z", "t_confirm": "2026-09-21T18:00:00Z",
               "z_narr": 2.8, "rank": 5, "moni_change": 3.0}]
    e = make_event(kind="exit", prev=10000.0, new=0.0,
                   t_lo="2026-09-21T06:00:00Z", t_hi="2026-09-21T12:00:00Z")
    c = classifier.classify(e, onsets=onsets, exchange_netflow_usd=500.0, divergence=0.5)
    assert c["cls"] == "distribution"


def test_distribution_null_fallback():
    """v1.5: exchange_net_flow_usd None → divergence < −1.5 alone decides."""
    onsets = [{"slug": "x", "token": "0x42af...", "chain": "robinhood",
               "t_onset": "2026-09-21T12:00:00Z", "t_confirm": "2026-09-21T18:00:00Z",
               "z_narr": 2.8, "rank": 5, "moni_change": 3.0}]
    e = make_event(kind="exit", prev=10000.0, new=0.0,
                   t_lo="2026-09-21T06:00:00Z", t_hi="2026-09-21T12:00:00Z")
    c = classifier.classify(e, onsets=onsets, exchange_netflow_usd=None, divergence=-2.0)
    assert c["cls"] == "distribution"
    # and NOT distribution when divergence is benign
    c2 = classifier.classify(e, onsets=onsets, exchange_netflow_usd=None, divergence=0.3)
    assert c2["cls"] != "distribution"


def test_score_distribution_formula():
    s = classifier.composite_score(
        {"delta_usd": 9000.0}, "distribution", None, tier_weight=1.0,
        z_delta_usd=2.0, divergence=-1.5, z_narr=3.5)
    # 0.4*(2/3) + 0.3*(min(1.5,3)/3) + 0.2*1.0 + 0.1*(1.5/3)
    expected = 0.4 * (2.0 / 3.0) + 0.3 * (1.5 / 3.0) + 0.2 * 1.0 + 0.1 * (1.5 / 3.0)
    assert abs(s - expected) < 1e-9
    assert s >= 0.55  # threshold reachable


def test_score_stealth_lead_formula():
    s = classifier.composite_score(
        {"delta_usd": 9000.0}, "stealth_lead", lead_hours=36.0, tier_weight=1.0,
        z_delta_usd=2.0, divergence=1.5)
    expected = 0.4 * (2.0 / 3.0) + 0.3 * (36.0 / 72.0) + 0.2 * 1.0 + 0.1 * (1.5 / 3.0)
    assert abs(s - expected) < 1e-9
    assert s >= 0.6


def test_budget_gate_math():
    from prelude import budget
    # v1.6 floors applied to the RAW balance
    assert budget.cadence_mode(106286) == "full"
    assert budget.cadence_mode(20000) == "full"
    assert budget.cadence_mode(4000) == "fi_12h"      # <5,000
    assert budget.cadence_mode(5000) == "full"        # boundary
    assert budget.cadence_mode(1999) == "balances_only"  # <2,000
    assert budget.cadence_mode(2000) == "fi_12h"      # boundary
    # demo reserve (1,000c) enforced at spend-time
    assert budget.usable_credits(1300) == 300
    assert budget.usable_credits(500) == 0
    assert budget.DEMO_RESERVE_CREDITS == 1000
    assert budget.MAX_DAY == 1500
    assert budget.MAX_7D == 8000


def test_demo_end_to_end_produces_png():
    from prelude import demo
    dbfile = os.path.join(HERE, "out", "test_demo.db")
    if os.path.exists(dbfile):
        os.remove(dbfile)  # fresh schema each run (old pre-migration DBs lack snapshot_id)
    results, png = demo.run(db_path=dbfile)
    assert os.path.exists(png)
    assert any(r["cls"] == "stealth_lead" for r in results)
