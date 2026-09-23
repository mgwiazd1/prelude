"""Backtest fixture tests — fake client + temp moni.db, no network, no key."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prelude import backtest, db  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = "0xB10CCa1"          # a "roster" wallet (tracked in the temp DB)
W2 = "0xB10CCa2"
W3 = "0xB10CCa3"


def _seed_db(tmp_path):
    """Temp prelude DB: 3 tracked active wallets, temp moni.db with onsets."""
    dbfile = str(tmp_path / "bt.db")
    conn = db.connect(dbfile)
    roster = {"roster": [
        {"address": W, "name": "w1", "chain": "solana", "status": "active"},
        {"address": W2, "name": "w2", "chain": "solana", "status": "active"},
        {"address": W3, "name": "w3", "chain": "solana", "status": "cold"},
    ]}
    rpath = str(tmp_path / "roster.json")
    json.dump(roster, open(rpath, "w"))
    os.environ["PRELUDE_ROSTER_PATH"] = rpath
    # track W and W2 in balance_snapshots (W3 = cold, untracked)
    for a in (W, W2):
        conn.execute(
            "INSERT OR REPLACE INTO balance_snapshots "
            "(snapshot_id, ts, address, chain, token, symbol, value_usd, "
            " price_usd, token_amount) VALUES (1,'2026-09-22T00:00:00',?,?,?,?,?,?,?)",
            (a, "solana", "T0", "TOK", 1000.0, None, None))
    conn.execute("INSERT INTO snapshots (id, ts, wallets) VALUES (1, '2026-09-22T00:00:00', 2)")
    conn.commit()
    # temp moni.db with 3 onsets (EVM-style 0x addresses, mcap-bearing)
    moni = str(tmp_path / "moni.db")
    m = sqlite3.connect(moni)
    m.execute("CREATE TABLE outcome_snapshots (id INTEGER PRIMARY KEY, "
              "source TEXT, source_ref TEXT, trigger_ts TEXT, symbol TEXT, "
              "contract_address TEXT, chain TEXT, price_at_trigger REAL, "
              "liquidity_at_trigger REAL, mcap_at_trigger REAL, "
              "trigger_fields TEXT, created_at TEXT)")
    for sym, ca, mcap, t, created in [
        ("SPK1", "AA1", 2_000_000, "2026-09-10T12:00:00Z", "2026-09-10T13:00:00Z"),
        ("SPK2", "AA2", 3_000_000, "2026-09-11T12:00:00Z", "2026-09-11T13:00:00Z"),
        ("SPK3", "AA3", 1_500_000, "2026-09-12T12:00:00Z", "2026-09-12T13:00:00Z"),
    ]:
        m.execute("INSERT INTO outcome_snapshots (source, source_ref, trigger_ts,"
                  " symbol, contract_address, chain, price_at_trigger,"
                  " liquidity_at_trigger, mcap_at_trigger, trigger_fields,"
                  " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ("test", "ev:1", t, sym, ca, "solana", 1.0, 1000.0, mcap,
                   "{}", created))
    m.commit()
    m.close()
    return conn, moni


class FakeBTClient:
    """Fake historical-who-bought-sold.

    W (tracked) buys in every SPIKE window; W2 (tracked) buys in every
    NULL window; W3 (cold/untracked) buys in spike windows but must be
    EXCLUDED from hits because it is not in the active+tracked wallet set.
    `spike_keys` = {(token, w_from, w_to)} for the spike windows — the
    discriminator, read from the DB by the test (the fake cannot tell a
    spike from its matched null by timestamp alone).
    """
    credits_remaining = 9999

    def __init__(self, spike_keys):
        self.calls = []
        self.spike_keys = spike_keys

    def historical_who_bought_sold(self, chain, token, date_from, date_to,
                                   context_note=None):
        self.calls.append((token, date_from, date_to))
        rows = []
        is_spike = (token, date_from, date_to) in self.spike_keys
        if is_spike:
            rows.append({"address": W, "address_label": "Smart Trader",
                         "is_smart_money": True,
                         "bought_volume_usd": 5000.0, "sold_volume_usd": 100.0,
                         "gross_volume_usd": 5100.0})
            rows.append({"address": W3, "address_label": "Smart Trader",
                         "is_smart_money": True,
                         "bought_volume_usd": 9000.0, "sold_volume_usd": 0.0,
                         "gross_volume_usd": 9000.0})  # untracked — must be excluded
        else:
            rows.append({"address": W2, "address_label": "Smart Trader",
                         "is_smart_money": True,
                         "bought_volume_usd": 4000.0, "sold_volume_usd": 0.0,
                         "gross_volume_usd": 4000.0})  # null-window hit for W2
        return {"data": rows, "pagination": {"total": len(rows)}}


def _spike_keys(conn):
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT token, w_from, w_to FROM backtest_windows WHERE kind='spike'")}


def test_select_accumulates_windows(tmp_path):
    conn, moni = _seed_db(tmp_path)
    n = backtest.select_spikes(conn, moni_db=moni, days=30, mcap_min=100_000)
    assert n == 6, f"3 onsets x (spike+null) = 6 windows, got {n}"
    kinds = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM backtest_windows GROUP BY kind").fetchall())
    assert kinds == {"spike": 3, "null": 3}
    # idempotent re-select adds nothing (UNIQUE on token,kind,w_from,w_to)
    n2 = backtest.select_spikes(conn, moni_db=moni)
    assert n2 == 0


def test_run_and_lift(tmp_path):
    conn, moni = _seed_db(tmp_path)
    backtest.select_spikes(conn, moni_db=moni)
    client = FakeBTClient(_spike_keys(conn))
    executed, credits = backtest.run_windows(conn, client=client)
    assert executed == 6 and credits == 9999
    # W hits all 3 spikes; W2 hits all 3 nulls; W3 (untracked) hits nothing
    st = backtest.stats(conn)
    assert st["spike_windows"] == 3 and st["spike_hits"] == 3
    assert st["null_windows"] == 3 and st["null_hits"] == 3
    # lift = (3/3)/(3/3) = 1.0 — the fixture is a no-edge case by design
    assert st["lift"] == 1.0
    # untracked cold wallet must NOT appear in hits
    for row in conn.execute("SELECT hits FROM backtest_windows"):
        assert W3 not in (row[0] or ""), "cold/untracked wallet leaked into hits"
    # no leads detected -> median/IQR None, n_leads 0
    assert st["median_lead_hours"] is None and st["n_leads"] == 0
    # per-wallet artifact: W 3/0 spike/null, W2 0/3
    out, path = backtest.artifact(conn, path=str(tmp_path / "art.json"))
    per = {e["address"]: e for e in out["per_wallet"]}
    assert per[W]["spike_hits"] == 3 and per[W]["null_hits"] == 0
    assert per[W2]["spike_hits"] == 0 and per[W2]["null_hits"] == 3
    assert W3 not in per


def test_lift_math_with_leads(tmp_path):
    conn, moni = _seed_db(tmp_path)
    backtest.select_spikes(conn, moni_db=moni)
    client = FakeBTClient(_spike_keys(conn))
    backtest.run_windows(conn, client=client)
    # pin leads on 4 spike windows to exercise median + IQR
    conn.execute("UPDATE backtest_windows SET lead_hours=6.0 WHERE kind='spike'")
    conn.execute("UPDATE backtest_windows SET lead_hours=12.0 WHERE kind='spike' AND id=(SELECT MIN(id) FROM backtest_windows)")
    st = backtest.stats(conn)
    assert st["n_leads"] == 3
    assert st["median_lead_hours"] == 6.0
    # 3 leads [6,6,12] -> inclusive quantiles q1=6, q3=9
    assert st["iqr_lead_hours"] == [6.0, 9.0]
