"""Demo mode — full pipeline from fixtures, no key, no network.

snapshot → diff → classify → score → receipt PNG in out/.
"""
import json
import os

from . import classifier, db, diff, narrative, receipt

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(HERE, "data", "fixtures")
OUT = os.path.join(HERE, "out")


def run(db_path=None):
    if db_path is None:
        # fixture mode is deterministic: never build on a previous run's DB
        # (a pre-snapshot_id demo.db broke `make demo` on 2026-09-23)
        db_path = os.path.join(OUT, "demo.db")
        if os.path.exists(db_path):
            os.remove(db_path)
    conn = db.connect(db_path)
    snaps = json.load(open(os.path.join(FIXTURES, "snapshots.json")))["snapshots"]
    onsets = narrative.load()
    backtest = json.load(open(os.path.join(FIXTURES, "backtest.json")))

    # 1. snapshot load — each fixture snapshot gets its own snapshot_id
    for s in snaps:
        cur = conn.execute(
            "INSERT INTO snapshots (ts, wallets) VALUES (?,?)",
            (s["ts"], len(s["balances"])))
        sid = cur.lastrowid
        for addr, rows in s["balances"].items():
            for r in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO balance_snapshots"
                    " (snapshot_id, ts, address, chain, token, symbol, value_usd, price_usd, token_amount)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, s["ts"], addr, r["chain"], r["token"], r.get("symbol"),
                     r["value_usd"], r.get("price_usd"), r.get("token_amount")),
                )
    # 2. diff
    events = diff.diff_snapshots(snaps[0], snaps[1])
    for e in events:
        conn.execute(
            "INSERT INTO delta_events (address, chain, token, symbol, kind, delta_usd, prev_usd, new_usd, t_lo, t_hi)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e["address"], e["chain"], e["token"], e["symbol"], e["kind"],
             e["delta_usd"], e["prev_usd"], e["new_usd"], e["t_lo"], e["t_hi"]),
        )
    # 3. classify + 4. score
    # synthetic wallets: fixture activity must never be attributed to a real
    # address (roster.example.json holds real exchange/public examples)
    roster = json.load(open(os.path.join(FIXTURES, "roster.demo.json")))["roster"]
    weights = {w["address"]: w["tier_weight"] for w in roster}
    baseline = {}
    for o in onsets:
        if "baseline" in o:
            baseline[o["token"]] = o["baseline"]["typical_move_usd"]
    results = []
    for e in events:
        c = classifier.classify(e, onsets=onsets, exchange_netflow_usd=None, divergence=None)
        onset = next((o for o in onsets if o["token"] == e["token"]), None)
        z_delta = e["delta_usd"] / baseline.get(e["token"], abs(e["delta_usd"]) or 1)
        z_narr = onset.get("z_narr") if onset else None
        s = classifier.composite_score(e, c["cls"], c["lead_hours"],
                                       weights.get(e["address"], 0.5), z_delta,
                                       z_narr=z_narr) if c["cls"] in ("stealth_lead", "distribution") else None
        results.append({**e, **c, "score": s})
        conn.execute(
            "INSERT INTO scores (delta_id, cls, score, lead_hours, divergence, z_delta_usd, z_narr, exchange_netflow_usd)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (conn.execute("SELECT id FROM delta_events WHERE t_hi=? AND token=? AND address=?",
                          (e["t_hi"], e["token"], e["address"])).fetchone()[0],
             c["cls"], s, c["lead_hours"], None, z_delta, z_narr, None),
        )
    conn.commit()
    # 5. receipt card — lift-first
    w0 = backtest["per_wallet"][0]
    png = receipt.render(
        w0["address"], "SCHIFFY",
        receipt.lift_stat(w0["spike_hits"], backtest["spike_windows"],
                          w0["control_hits"], backtest["control_windows"]),
        w0["median_lead_hours"], 31.0,
        os.path.join(OUT, "receipt_demo.png"),
        iqr_h=w0.get("iqr_lead_hours"),
    )
    print(f"PRELUDE demo — classified {len(results)} events from fixtures "
          "(SYNTHETIC fixture data: shows the pipeline, not a result)")
    for r in results:
        line = (f"  {r['t_hi']}  {r['address'][:10]}…  {r['kind']:5s} "
                f"${r['delta_usd']:>10,.0f}  cls={r['cls']}")
        if r["lead_hours"] is not None:
            line += f"  lead={r['lead_hours']:.0f}h"
        if r["score"] is not None:
            line += f"  S={r['score']:.2f}"
        print(line)
    stealth = [r for r in results if r["cls"] == "stealth_lead"]
    if stealth:
        print(f"  → stealth_lead alert fired: {stealth[0]['symbol']} by {stealth[0]['address'][:10]}…")
    print(f"  receipt card: {png}")
    return results, png
