"""Backfill per-(wallet, token) entry history from historical-balances.

Why: balance_snapshots first-appearance = poller START time, not entry time.
A check verdict that reads snapshot order as "entry" is reading our own
instrument's start date. balance_history (this table) is the CORRECT first_seen
source.

Source: /api/v1/profiler/address/historical-balances (standard, 1c/page,
FULL balance history — NOT top-N, DAILY resolution, 90d+ lookback — all
verified live 2026-09-22).

Cost (measured, 2026-09-22): ~3 pages per active wallet over 90d (49 tokens,
2,441 rows) → ~135 credits for the 45-wallet roster; worst case ~225.

Idempotent: backfill_runs records completed (address, range_from); re-runs
skip done wallets. Feeds BOTH check (first_seen) and conviction_hold (span).
"""
import json
import os
from datetime import datetime, timedelta, timezone

from . import db, nansen_client


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def tracked_wallets(conn, roster_path=None):
    """Active roster wallets that actually appear in balance_snapshots."""
    roster_path = roster_path or os.environ.get(
        "PRELUDE_ROSTER_PATH", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "roster.json"))
    with open(roster_path) as f:
        roster = json.load(f)
    entries = roster.get("roster", roster if isinstance(roster, list) else [])
    active = {w["address"] for w in entries if w.get("status") == "active"}
    tracked = {r[0] for r in conn.execute(
        "SELECT DISTINCT address FROM balance_snapshots")}
    return sorted(active & tracked)


def backfill_wallet(client, conn, address, chain="solana",
                    range_from="2026-06-22", range_to=None):
    """Fetch one wallet's full history, insert rows, record the run.

    Returns (n_rows, n_pages, credits_used).
    """
    range_to = range_to or _iso(datetime.now(timezone.utc)).split("T")[0]
    start_credits = client.credits_remaining
    rows, pages = client.historical_balances(
        address, chain, range_from, range_to,
        context_note=f"backfill {address[:6]} {range_from}..{range_to}")
    conn.executemany("""
        INSERT OR REPLACE INTO balance_history
        (address, chain, token, token_symbol, block_timestamp,
         token_amount, value_usd)
        VALUES (?,?,?,?,?,?,?)""",
        [(address, chain, r["token_address"], r.get("token_symbol"),
          r["block_timestamp"], r.get("token_amount"),
          float(r.get("value_usd") or 0)) for r in rows])
    credits = (start_credits - client.credits_remaining) \
        if (start_credits and client.credits_remaining) else pages
    conn.execute("""INSERT OR REPLACE INTO backfill_runs
        (address, range_from, range_to, pages, rows, credits, status,
         finished_ts) VALUES (?,?,?,?,?,?,?,?)""",
        (address, range_from, range_to, pages, len(rows), credits, "done",
         _iso(datetime.now(timezone.utc))))
    conn.commit()
    return len(rows), pages, credits


def backfill_all(client, conn, roster_path=None,
                 range_from="2026-06-22", range_to=None, limit=None):
    """Backfill every tracked wallet (idempotent, resumable).

    Returns summary dict. Wallets already done for range_from are skipped.
    """
    wallets = tracked_wallets(conn, roster_path)
    done = {r[0] for r in conn.execute(
        "SELECT address FROM backfill_runs WHERE range_from=? AND status='done'",
        (range_from,))}
    todo = [w for w in wallets if w not in done]
    if limit:
        todo = todo[:limit]
    summary = {"wallets_tracked": len(wallets), "already_done": len(done),
               "planned": len(todo), "done_now": 0, "skipped": 0,
               "failed": 0, "rows": 0, "pages": 0, "credits": 0}
    for w in todo:
        try:
            n_rows, pages, credits = backfill_wallet(
                client, conn, w, range_from=range_from, range_to=range_to)
        except Exception as e:
            summary["failed"] += 1
            conn.execute("""INSERT OR REPLACE INTO backfill_runs
                (address, range_from, range_to, status)
                VALUES (?,?,?, 'error')""", (w, range_from, range_to or ""))
            conn.commit()
            print(f"  FAIL {w[:6]}: {str(e)[:120]}")
            continue
        summary["done_now"] += 1
        summary["rows"] += n_rows
        summary["pages"] += pages
        summary["credits"] += credits
        print(f"  {w[:6]}: {n_rows} rows / {pages} pages / {credits}c")
    summary["credits_remaining"] = client.credits_remaining
    return summary


def entry_first_seen(conn, tokens=None):
    """first_seen per (wallet, token) from BACKFILLED history only.

    Entry = first timestamp with value_usd > 0 (zero-balance rows before
    entry are not entries). Returns {(address, token): block_timestamp}.
    """
    sql = ("SELECT address, token, MIN(block_timestamp) fs FROM balance_history "
           "WHERE value_usd > 0 GROUP BY address, token")
    if tokens:
        marks = ",".join("?" for _ in tokens)
        sql += f" AND token IN ({marks})"
        args = list(tokens)
    else:
        args = []
    return {(a, t): fs for a, t, fs in conn.execute(sql, args).fetchall()}
