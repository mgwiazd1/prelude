"""Poller: roster → current-balance snapshots, **3h cadence (8 passes/day)**, budget-gated.

Runs under `make run`. Reads the roster JSON (default data/roster.example.json —
the real operator roster lives OUTSIDE the repo and is loaded from
PRELUDE_ROSTER path when set).
"""
import json
import os
from datetime import datetime, timezone

import httpx

from . import budget, db
from .nansen_client import NansenClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# MG roster contract: private roster at data/roster.json (gitignored); the
# committed example is the demo/default so `make demo` runs with zero config.
ROSTER_PATH = os.environ.get("PRELUDE_ROSTER_PATH",
                             os.path.join(HERE, "data", "roster.json"))
if not os.path.exists(ROSTER_PATH):
    ROSTER_PATH = os.path.join(HERE, "data", "roster.example.json")


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "")


def load_roster(path=None):
    """Load + normalize the roster. Addresses are stripped — the upstream
    tracker has trailing-space addresses (422/500 'Invalid parameter').
    Dedupes on stripped address (first entry wins)."""
    with open(path or ROSTER_PATH) as f:
        roster = json.load(f)["roster"]
    seen, out = set(), []
    for w in roster:
        w = dict(w)
        w["address"] = str(w["address"]).strip()
        key = (w["address"], w.get("chain", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def _short(a):
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def snapshot_all(client=None, conn=None, roster=None, ts=None, live_view=False):
    """One snapshot pass over the active roster. Returns (n_wallets, credits).

    The pass gets ONE snapshot_id (monotonic, from the snapshots table) —
    this is the ONLY grouping key for diffs. `ts` is a display column only
    (a shared timestamp breaks on overlapping passes / clock changes /
    restarts; snapshot_id does not). A 200 with zero rows is recorded as a
    verified-empty marker row (token='__empty__') so the partition
    (stored-with-rows / stored-empty / dead-letter) is DB-recoverable.
    A failed (non-200) wallet writes NO row.
    """
    conn = conn or db.connect()
    if client is None:
        client = NansenClient(caller="prelude")
    roster = roster if roster is not None else load_roster()
    active = [w for w in roster if w.get("status", "active") == "active"]
    ts = ts or _now_iso()  # pass-level display timestamp
    status = budget.gate_status("prelude")
    if not status["ok"]:
        print(f"[poller] budget gate closed ({status}) — skipping snapshot")
        return 0, client.credits_remaining
    mode = budget.cadence_mode(client.credits_remaining)
    # one snapshot_id per pass — the diff/group key
    cur = conn.execute(
        "INSERT INTO snapshots (ts, wallets) VALUES (?,?)", (ts, len(active)))
    snapshot_id = cur.lastrowid
    conn.commit()
    if live_view:
        print(f"[poller] budget gate ok: {status['today']}/{status['day_cap']} today, "
              f"{status['week']}/{status['week_cap']} this week · {len(active)} wallets")
    else:
        print(f"[poller] gate={status} mode={mode} wallets={len(active)} "
              f"snapshot_id={snapshot_id} ts={ts}")
    n = 0
    dead = []
    for w in active:
        if not budget.gate_ok("prelude"):
            print("[poller] day/week cap hit mid-pass — stopping")
            break
        try:
            data = client.current_balance(w["address"], w["chain"])
        except (RuntimeError, httpx.HTTPStatusError) as e:
            if live_view:
                print(f"  POST /api/v1/profiler/address/current-balance  "
                      f"{client.last_status or 'ERR'}  {w['chain']:<9} "
                      f"{_short(w['address'])}  -> dead-letter: {str(e)[:60]}")
            # failure table: 422/5xx per-object dead-letter (5xx after its 1 retry)
            # — pass continues. A failed call NEVER writes a row (not even an
            # empty marker), so it cannot be confused with a verified-empty 200.
            dead.append((w["address"], str(e)[:120]))
            print(f"[poller] dead-letter {w.get('name', w['address'])[:12]}…: {e}")
            continue
        rows = data.get("data", []) or []
        if live_view:
            top = max(rows, key=lambda r: float(r.get("value_usd") or 0), default=None)
            top_s = (f"  top {top.get('token_symbol') or top.get('symbol')} "
                     f"${float(top.get('value_usd') or 0):,.0f}") if top else ""
            print(f"  POST /api/v1/profiler/address/current-balance  "
                  f"{client.last_status}  {client.last_cost or '?'} credit  "
                  f"{len(rows):>3} rows  {w['chain']:<9} {_short(w['address'])}"
                  f"  {client.last_ms}ms{top_s or '  (verified empty)'}")
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO balance_snapshots"
                " (snapshot_id, ts, address, chain, token, symbol, value_usd, price_usd, token_amount)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (snapshot_id, ts, w["address"], w["chain"],
                 r.get("token_address") or "native",
                 r.get("token_symbol") or r.get("symbol"),
                 float(r.get("value_usd") or 0.0),
                 r.get("price_usd"), r.get("token_amount")),
            )
        if not rows:
            # verified-empty: 200 with zero data rows
            conn.execute(
                "INSERT OR REPLACE INTO balance_snapshots"
                " (snapshot_id, ts, address, chain, token, symbol, value_usd, price_usd, token_amount)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (snapshot_id, ts, w["address"], w["chain"], "__empty__", None, 0.0, None, None),
            )
        n += 1
    conn.execute(
        "INSERT INTO credits_log (credits_remaining, caller) VALUES (?,?)",
        (client.credits_remaining or 0, "prelude"),
    )
    conn.commit()
    summary = (f"[poller] pass complete: snapshot_id={snapshot_id}, {n} wallets, "
               f"{len(dead)} dead-letter, credits={client.credits_remaining}")
    print(summary)
    if dead:
        for addr, err in dead:
            print(f"  dead-letter {addr[:12]}…: {err}")
    return n, client.credits_remaining
