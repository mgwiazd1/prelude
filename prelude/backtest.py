"""Layer 1 — Lead-Reliability Backtest (spec §D.6, v1.6: 100 spikes + 100 null,
6h windows, lift + median-lead/IQR).

Two honest facts discovered while scaffolding (2026-09-22, verified live):

1. **The endpoint returns per-address AGGREGATES per window, not per-tx
   timestamps** (`TGMHistoricalWhoBoughtSold`: bought/sold_volume_usd summed
   over date_range; no block_timestamp). A window query answers "did this
   roster wallet net-buy in the 6h pre-onset window?" — it is a HIT/MISS
   query. LEAD TIME comes from our own snapshot diffs (3h resolution,
   tx-pinned), NOT from this endpoint. The spec's "median lead 14h (IQR)"
   phrasing was written on the assumption of per-tx data; the IQR-on-lead
   statistic is computed from detected snapshot-diff entries and is
   populated only when at least one entry exists.

2. **Spike selection is onset-bound.** A spike window requires a narrative
   onset on a queryable token. `outcome_snapshots` (moni.db, 4,538 events /
   30d) is EVM-dominant (mcap-bearing: Bonk/EDEL/Rabbit/PRINTER/DTF/AIAIAI/
   SPACEHOOD…); Solana coverage is thin (1 pump token >$100k mcap in 30d).
   So the backtest runs INCREMENTALLY: `select` accumulates eligible windows
   in the backtest_windows table; `run` executes whatever is accumulated.
   The 100+100 target is reached when onset coverage provides the windows —
   not by lowering the bar to hit the number early.

Endpoint (verified live 2026-09-22, 1c/call, 6h + 7d windows both 200):
  POST /api/v1beta1/tgm/historical-who-bought-sold
  {chain, token_address, date_range:{from,to}, pagination}
  -> data[]: address, address_label (temporally resolved at date_to — the
     look-ahead fix for F5), is_smart_money, bought/sold/gross_volume_usd.

Null control (v1.5 rule): 100 MATCHED non-spike windows — same token
(preferred) or mcap-matched token, same wallet set, same 6h width. Lift =
(hit_rate_spike / hit_rate_control). Receipt card stat is the LIFT.

Metering: caller='prelude_backtest' (out of the prelude day-gate, same
service='nansen' evidence query per spec §G).
"""
import json
import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from . import db, nansen_client

MONI_DB = os.environ.get(
    "PRELUDE_MONI_DB", "/home/proxmox/remi-intelligence/moni.db")
WINDOW_HOURS = 6
HIT_MIN_USD = 1000.0   # net-buy above this counts as a roster hit
STABLES = {"So11111111111111111111111111111111111111112",
           "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDtV",
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def active_wallets(conn):
    roster_path = os.environ.get("PRELUDE_ROSTER_PATH",
                                 os.path.join(os.path.dirname(
                                     os.path.dirname(os.path.abspath(__file__))),
                                     "data", "roster.json"))
    with open(roster_path) as f:
        roster = json.load(f)["roster"]
    addrs = {w["address"].strip() for w in roster
             if w.get("status") == "active"}
    # only wallets we actually track in the DB count as "our wallet set"
    tracked = {r[0] for r in conn.execute(
        "SELECT DISTINCT address FROM balance_snapshots")}
    return sorted(addrs & tracked)


CAP_SPIKES = 100   # spec: 100 spikes + 100 nulls; do not over-spend credits
CAP_NULLS = 100


def _count_by_kind(conn):
    return dict(conn.execute(
        "SELECT kind, COUNT(*) FROM backtest_windows GROUP BY kind").fetchall())


def select_spikes(conn, moni_db=None, days=30, mcap_min=100_000,
                  chain="solana", verbose=True):
    """Accumulate eligible spike + matched null windows in backtest_windows.

    Spike: narrative onset (outcome_snapshots trigger, mcap-bearing,
    non-stable, chain-matched to the roster) with a 6h pre-onset window.
    Null: matched window on the same token with no onset in that window
    (preferred) or an mcap-matched token. Returns counts of new rows.

    n is printed at every stage before any run credit is spent:
      stage 1 = onsets enumerated (from the onset source)
      stage 2 = onsets that produced windows (after caps)
      stage 3 = total windows in the table by kind
    If onsets ≫ the cap, we take the first CAP_SPIKES spikes and stop —
    credits are not spent just because they allow more.

    chain: 'solana' keeps non-0x onsets only (default — the active roster
    is Solana-only, and who-bought-sold hits on a 0x token would all miss
    against Solana addresses, silently zeroing the experiment). 'ethereum'
    keeps 0x only; 'both' = no filter.
    """
    moni = sqlite3.connect(moni_db or MONI_DB)
    moni.row_factory = None
    conn.row_factory = None
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    if chain == "solana":
        chain_f = "AND contract_address NOT GLOB '0x*'"
    elif chain == "ethereum":
        chain_f = "AND contract_address GLOB '0x*'"
    else:
        chain_f = ""
    onsets = moni.execute(f"""
        SELECT contract_address, MAX(mcap_at_trigger) mcap,
               MIN(trigger_ts) t_onset, symbol
        FROM outcome_snapshots
        WHERE created_at >= ? AND contract_address IS NOT NULL
          AND mcap_at_trigger > ? AND symbol NOT IN
          ('USDC','USDT','USDS','DAI','WBTC','WETH','SOL','BTC','ETH')
          {chain_f}
          AND contract_address NOT IN
          ('DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263',
           'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v')
        GROUP BY contract_address HAVING MIN(trigger_ts) >= ?
    """, (since, mcap_min, since)).fetchall()
    addrs = active_wallets(conn)
    # chain inference: 0x… -> ethereum (Nansen historical supports
    # base/bnb/ethereum/solana); otherwise solana for pump/long base58
    def chain_of(ca):
        return "ethereum" if ca.startswith("0x") else "solana"
    n_new = 0
    n_onsets = len(onsets)
    if verbose:
        print(f"  stage 1: onsets enumerated = {n_onsets} (chain={chain}, "
              f"days={days}, mcap_min=${mcap_min:,})")
    n_used = 0
    n_capped = 0
    for ca, mcap, t_onset, sym in onsets:
        # cap check BEFORE building this onset's windows: if we already have
        # the full spec allocation, stop — do not spend run credits on extra.
        cur = _count_by_kind(conn)
        if (cur.get("spike", 0) >= CAP_SPIKES
                and cur.get("null", 0) >= CAP_NULLS):
            n_capped = n_onsets - n_used
            break
        n_used += 1
        t_on = _parse(t_onset)
        windows = [
            ("spike", t_on - timedelta(hours=WINDOW_HOURS), t_on, None),
            # matched null: same token, one day earlier, 6h window with no
            # onset inside it
            ("null", t_on - timedelta(hours=WINDOW_HOURS + 24),
             t_on - timedelta(hours=24), ca),
        ]
        for kind, w0, w1, match in windows:
            ins = conn.execute("""INSERT OR IGNORE INTO backtest_windows
                (token, chain, symbol, mcap_usd, kind, w_from, w_to,
                 match_token, status, created_ts, roster_size)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ca, chain_of(ca), sym, mcap, kind, _iso(w0), _iso(w1),
                 match, "pending", _iso(datetime.now(timezone.utc)),
                 len(addrs)))
            n_new += ins.rowcount
    conn.commit()
    moni.close()
    final = _count_by_kind(conn)
    if verbose:
        capped = f"capped at {CAP_SPIKES}+{CAP_NULLS}" if n_capped else "all"
        print(f"  stage 2: onsets used = {n_used} ({capped})")
        print(f"  stage 3: windows now in table — "
              f"spike={final.get('spike', 0)}/{CAP_SPIKES}, "
              f"null={final.get('null', 0)}/{CAP_NULLS} "
              f"(+{n_new} new)")
    return n_new


def run_windows(conn, client=None, limit=None, verbose=True):
    """Execute pending windows via historical-who-bought-sold.

    Hit rule: any active-roster wallet with bought_volume_usd −
    sold_volume_usd > HIT_MIN_USD inside the window.
    Returns (n_executed, credits_remaining).
    """
    client = client or nansen_client.NansenClient(caller="prelude_backtest")
    conn.row_factory = None
    addrs = set(active_wallets(conn))
    rows = conn.execute("""SELECT id, token, chain, kind, w_from, w_to
        FROM backtest_windows WHERE status='pending'
        ORDER BY created_ts LIMIT ?""",
        (limit or 10**9,)).fetchall()
    if verbose:
        pend = {}
        for r in rows:
            pend[r[3]] = pend.get(r[3], 0) + 1
        try:
            credits = client.credits_remaining
        except Exception:
            credits = "?"
        print(f"  pre-spend: {len(rows)} pending windows to run "
              f"(spike={pend.get('spike', 0)}, null={pend.get('null', 0)}), "
              f"credits before = {credits}, "
              f"~{len(rows)} credits to spend at 1c/window")
    executed = 0
    for wid, token, chain, kind, w0, w1 in rows:
        try:
            resp = client.historical_who_bought_sold(
                chain, token, w0, w1)
        except Exception as e:
            conn.execute("UPDATE backtest_windows SET status='error', "
                         "error=? WHERE id=?", (str(e)[:200], wid))
            continue
        data = resp.get("data") or []
        hits, net_usd = [], 0.0
        for r in data:
            if r.get("address") not in addrs:
                continue
            net = float(r.get("bought_volume_usd") or 0) - \
                float(r.get("sold_volume_usd") or 0)
            if net > HIT_MIN_USD:
                hits.append({"address": r["address"], "label":
                             r.get("address_label"), "net_usd": round(net, 2)})
                net_usd = max(net_usd, net)
        conn.execute("""UPDATE backtest_windows
            SET status='done', hits=?, net_buy_usd=?, n_smart_rows=?,
                executed_ts=? WHERE id=?""",
            (json.dumps(hits), net_usd, len(data),
             _iso(datetime.now(timezone.utc)), wid))
        executed += 1
    conn.commit()
    return executed, client.credits_remaining


def stats(conn, min_leads_for_iqr=1):
    """Lift + hit rates; median lead / IQR from snapshot-diff entries when
    detected leads exist (lead_hours is filled by tx-pinning, not by the
    window query)."""
    conn.row_factory = None
    def agg(kind):
        rows = conn.execute("""SELECT hits FROM backtest_windows
            WHERE status='done' AND kind=?""", (kind,)).fetchall()
        n_win = len(rows)
        n_hit = sum(1 for r in rows if json.loads(r[0] or "[]"))
        return n_win, n_hit
    sw, sh = agg("spike")
    cw, ch = agg("null")
    out = {"spike_windows": sw, "spike_hits": sh,
           "null_windows": cw, "null_hits": ch,
           "hit_rate_spike": round(sh / sw, 3) if sw else None,
           "hit_rate_null": round(ch / cw, 3) if cw else None,
           "lift": None, "median_lead_hours": None, "iqr_lead_hours": None,
           "n_leads": 0}
    if sw and cw and ch:
        out["lift"] = round((sh / sw) / (ch / cw), 2)
    leads = [r[0] for r in conn.execute(
        "SELECT lead_hours FROM backtest_windows WHERE lead_hours > 0")]
    if leads:
        out["n_leads"] = len(leads)
        out["median_lead_hours"] = round(statistics.median(leads), 1)
        if len(leads) >= min_leads_for_iqr:
            q = statistics.quantiles(leads, n=4, method="inclusive")
            out["iqr_lead_hours"] = [round(q[0], 1), round(q[2], 1)]
    out["artifact_ts"] = _iso(datetime.now(timezone.utc))
    return out


def artifact(conn, path=None):
    """Write the frozen stats artifact (spec §D.6 — ships lift, not count)."""
    st = stats(conn)
    per = []
    conn.row_factory = None
    addrs = {}
    for wid, kind, token, sym, hits, lead in conn.execute(
            "SELECT id, kind, token, symbol, hits, lead_hours "
            "FROM backtest_windows WHERE status='done'"):
        for h in json.loads(hits or "[]"):
            e = addrs.setdefault(h["address"],
                                 {"address": h["address"], "label":
                                  h.get("label"), "spike_hits": 0,
                                  "null_hits": 0, "leads": []})
            e["spike_hits" if kind == "spike" else "null_hits"] += 1
            if lead:
                e["leads"].append(lead)
    for a, e in addrs.items():
        if e["spike_hits"] + e["null_hits"] == 0:
            continue
        lead_hrs = e["leads"]
        e["median_lead_hours"] = round(statistics.median(lead_hrs), 1) \
            if lead_hrs else None
        e["iqr_lead_hours"] = (
            [round(statistics.quantiles(lead_hrs, n=4, method="inclusive")[0], 1),
             round(statistics.quantiles(lead_hrs, n=4, method="inclusive")[2], 1)]
            if len(lead_hrs) >= 4 else None) if lead_hrs else None
        per.append(e)
    out = {"stats": st, "per_wallet": sorted(
        per, key=lambda e: -(e["spike_hits"] + e["null_hits"]))}
    path = path or os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), "out",
        "backtest_artifact.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    return out, path
