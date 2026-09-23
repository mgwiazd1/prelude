"""check <token> — one command: "did the signal wallets get here before the crowd?"

Reads stored data (balance_history backfill + balance_snapshots + roster.json).
Zero API calls at query time — the backfill that populated balance_history was
the spend; this is the 30-second judge path.

THE ENTRY-TIME BUG (v1.8, 2026-09-22) — and the fix:
  balance_snapshots first-appearance is the POLLER START time, not when the
  wallet bought. With 3 same-day snapshots every verdict would read OUR OWN
  start date as "entry" — a correctness bug in the judge path, not just a weak
  demo. So first_seen now comes from balance_history (backfilled real entry
  history, /profiler/address/historical-balances). Snapshots remain for live
  deltas only.

HARD RULE — provenance gate:
  A LEAD CLAIM (SIGNAL_LEAD / COHORT_LEAD) is valid ONLY when BOTH the signal
  entry time and the cohort entry time came from backfilled history. If either
  side is snapshot-order (poller start) the comparison is meaningless and the
  verdict is DOWNGRADED to UNVERIFIED_ENTRY_TIMING — a snapshot-order verdict
  can never render as a lead claim. The provenance line always states which
  source each side's entry time came from.

GATE (lookup): exposure = any tracked holder present in our data (we poll it).
delta_likely gates ALERTING, not lookups — it is a secondary "signal"
attribute, reported not gating. The broader public crowd is NOT tracked; it is
reported as such, never implied.

Every number is printed with n (snapshot count, wallet count) so a stat is
never read off a bare ratio.
"""
import json
import os
from datetime import datetime

ROSTER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "roster.json")


def _load_roster(path=None):
    with open(path or ROSTER_PATH) as f:
        r = json.load(f)
    entries = r.get("roster", r if isinstance(r, list) else [])
    return {w.get("address"): w for w in entries if w.get("address")}


def _resolve_token(conn, token):
    """Return (symbol, address, chain) for a symbol or address."""
    sym = token.upper()
    row = conn.execute(
        "SELECT symbol, token, chain FROM balance_snapshots WHERE symbol=? OR token=? "
        "LIMIT 1", (sym, token)).fetchone()
    if row:
        return row["symbol"], row["token"], row["chain"]
    row = conn.execute(
        "SELECT token_symbol, token, chain FROM balance_history "
        "WHERE token_symbol=? OR token=? LIMIT 1", (sym, token)).fetchone()
    if row:
        return row["token_symbol"], row["token"], row["chain"]
    return sym, None, None


def _ts_to_dt(s):
    return datetime.fromisoformat((s or "").replace("Z", "+00:00"))


def check_token(conn, token, roster_path=None):
    symbol, address, chain = _resolve_token(conn, token)
    roster = _load_roster(roster_path)

    # entry times from both sources; backfill = real entry date,
    # snapshot = poller start (never a lead claim by itself)
    # NOTE: balance_history.token holds the ADDRESS, .token_symbol the symbol.
    # Query by resolved address; symbol input must NOT be passed to token=?.
    bf_first = {}
    if address:
        for a, fs in conn.execute(
                "SELECT address, MIN(block_timestamp) fs FROM balance_history "
                "WHERE token=? AND value_usd > 0 GROUP BY address", (address,)):
            bf_first[a] = fs
    elif symbol:
        for a, fs in conn.execute(
                "SELECT address, MIN(block_timestamp) fs FROM balance_history "
                "WHERE token_symbol=? AND value_usd > 0 GROUP BY address",
                (symbol.upper(),)):
            bf_first[a] = fs

    snap_rows = conn.execute(
        "SELECT snapshot_id, ts, address, value_usd FROM balance_snapshots "
        "WHERE symbol=? OR token=? ORDER BY snapshot_id",
        (symbol.upper(), token)).fetchall()
    snap_first = {}
    for r in snap_rows:
        if r["address"] not in snap_first and (r["value_usd"] or 0) > 0:
            snap_first[r["address"]] = (r["snapshot_id"], r["ts"])

    if not bf_first and not snap_rows:
        return {
            "token": token,
            "resolved": {"symbol": symbol, "address": address, "chain": chain},
            "verdict": "NO_TRACKED_EXPOSURE",
            "lead_claim_valid": False,
            "lead_hours": None,
            "n_snapshots": 0, "n_wallets_total_tracked": 0,
            "n_signal": 0, "n_cohort": 0,
            "provenance": {"signal": "none", "cohort": "none"},
            "note": "No tracked wallet holds this token in any stored data. "
                    "Not in our polling set — zero-cost path ends here.",
        }

    all_addrs = set(bf_first) | set(snap_first)

    def pop(addr):
        w = roster.get(addr)
        return "signal" if (w and w.get("delta_likely") is True) else "cohort"

    def earliest(popname):
        addrs = [a for a in all_addrs if pop(a) == popname]
        bf_hits = [(bf_first[a], a) for a in addrs if a in bf_first]
        if bf_hits:
            return min(bf_hits, key=lambda t: t[0])[0], "backfill"
        sn_hits = [(snap_first[a][0], snap_first[a][1])
                   for a in addrs if a in snap_first]
        if sn_hits:
            return min(sn_hits, key=lambda t: t[0])[1], "snapshot"
        return None, "none"

    sig_ts, sig_src = earliest("signal")
    coh_ts, coh_src = earliest("cohort")

    # LEFT-CENSORING: a backfill first_seen on the wallet's coverage start day
    # means "already held when history begins", not an entry. One censored
    # side keeps the ORDER but the lead is only a lower bound; both censored
    # = order unknown (was rendered CONCURRENT lead=0 — wrong, 2026-09-23).
    try:
        cov = dict(conn.execute("SELECT address, range_from FROM backfill_runs"))
    except Exception:
        cov = {}

    def censored(popname, ts, src):
        if src != "backfill" or not ts:
            return False
        return any(bf_first.get(a) == ts and cov.get(a) and ts[:10] <= cov[a]
                   for a in all_addrs if pop(a) == popname)

    sig_cens = censored("signal", sig_ts, sig_src)
    coh_cens = censored("cohort", coh_ts, coh_src)

    # entry SIZE per side: the earliest-entry wallet's day-1 balance (lower
    # bound, daily resolution) + peak position (upper bound). Always printed
    # with the verdict — no lead claim renders without its magnitude.
    def _size_for(popname, ts, src):
        # sorted: all_addrs is a set, and string-set order is randomised per
        # process — tied entries rendered a different wallet/size per run
        addrs = sorted(a for a in all_addrs if pop(a) == popname)
        if src == "backfill":
            best = None
            for a in addrs:
                if bf_first.get(a) == ts:
                    d1 = conn.execute(
                        "SELECT value_usd FROM balance_history "
                        "WHERE address=? AND token=? AND block_timestamp=? "
                        "AND value_usd>0", (a, address, ts)).fetchone()
                    pk = conn.execute(
                        "SELECT MAX(value_usd) FROM balance_history "
                        "WHERE address=? AND token=? AND value_usd>0",
                        (a, address)).fetchone()
                    # ties at the earliest entry: the LARGEST entry states
                    # "this side entered with at least $X" (tie-break: address)
                    if d1 is not None and (best is None or d1[0] > best["entry_usd"]):
                        best = {"wallet": (roster.get(a) or {}).get("name", a[:6]),
                                "address": a, "entry_usd": round(d1[0], 2),
                                "peak_usd": round(pk[0], 2) if pk
                                              else round(d1[0], 2)}
            if best:
                return best
        elif src == "snapshot":
            best = min((snap_first[a][0] for a in addrs if a in snap_first))
            for a in addrs:
                if a in snap_first and snap_first[a][0] == best:
                    row = conn.execute(
                        "SELECT value_usd FROM balance_snapshots "
                        "WHERE address=? AND token=? AND snapshot_id=?",
                        (a, address, best)).fetchone()
                    v = (row[0] or 0) if row else 0
                    if v > 0:
                        return {"wallet": (roster.get(a) or {}).get("name", a[:6]),
                                "address": a, "entry_usd": round(v, 2),
                                "peak_usd": round(v, 2)}
        return None

    sig_size = _size_for("signal", sig_ts, sig_src)
    coh_size = _size_for("cohort", coh_ts, coh_src)

    # HARD RULE: lead claim requires BOTH sides backfill-sourced
    if sig_ts is None and coh_ts is None:
        verdict, lead_hours, lead_ok = "NO_TRACKED_EXPOSURE", None, False
    elif sig_ts is None:
        verdict, lead_hours, lead_ok = "COHORT_ONLY", None, False
    elif coh_ts is None:
        verdict, lead_hours, lead_ok = "SIGNAL_ONLY", None, False
    elif sig_src == "backfill" and coh_src == "backfill" and sig_cens and coh_cens:
        verdict, lead_hours, lead_ok = "UNVERIFIED_ENTRY_TIMING", None, False
    elif sig_src == "backfill" and coh_src == "backfill":
        dh = (_ts_to_dt(coh_ts) - _ts_to_dt(sig_ts)).total_seconds() / 3600.0
        lead_hours = round(dh, 1)
        if dh > 1:
            verdict = "SIGNAL_LEAD"
        elif dh < -1:
            verdict = "COHORT_LEAD"
        else:
            verdict = "CONCURRENT"
        lead_ok = True
    else:
        verdict, lead_hours, lead_ok = "UNVERIFIED_ENTRY_TIMING", None, False

    # per-snapshot summary — live deltas only, informational
    per = {}
    for r in snap_rows:
        per.setdefault(r["snapshot_id"], {})[r["address"]] = (r["ts"],
                                                              r["value_usd"] or 0)
    snaps = []
    for s in sorted(per):
        h = per[s]
        snaps.append({"snapshot": s, "ts": list(h.values())[0][0],
                      "n_holders": len(h),
                      "n_signal": sum(1 for a in h if pop(a) == "signal"),
                      "tracked_value_usd": round(sum(v for _, v in h.values()), 2)})

    # top holders: backfilled peak value (real) with entry time + source
    top_list = []
    for a, v in sorted(((a, _peak(conn, a, address, symbol)) for a in all_addrs),
                       key=lambda x: -x[1])[:5]:
        top_list.append({
            "wallet": (roster.get(a) or {}).get("name", a[:6]),
            "address": a, "peak_usd": round(v, 2),
            "signal": pop(a) == "signal",
            "entry_ts": bf_first.get(a) or
                        (snap_first.get(a) and snap_first[a][1]),
            "entry_source": ("backfill" if a in bf_first
                             else ("snapshot" if a in snap_first else "none"))})

    n_sig = sum(1 for a in all_addrs if pop(a) == "signal")
    # size floor (matches the delta engine thresholds): a lead claim with a
    # sub-$1k first entry on either side is small-position — flagged, and
    # never rendered as conviction-scale.
    # `is not None`, not truthiness: a dust entry rounds to 0.0 and must pull
    # the floor DOWN, never drop out and let the other side set it.
    floors = [s["entry_usd"] for s in (sig_size, coh_size)
              if s and s.get("entry_usd") is not None]
    size_floor = None
    if floors and verdict in ("SIGNAL_LEAD", "COHORT_LEAD", "CONCURRENT"):
        m = min(floors)
        size_floor = ("5k" if m >= 5000 else
                      ("1k" if m >= 1000 else f"sub-{m:,.0f}"))
    return {
        "token": token,
        "resolved": {"symbol": symbol, "address": address, "chain": chain},
        "verdict": verdict,
        "lead_claim_valid": lead_ok,
        # rendered form: a censored side makes the claim a MINIMUM, not exact
        "lead_claim": ("lower_bound" if lead_ok and (sig_cens or coh_cens)
                       else "valid" if lead_ok else "none"),
        "lead_hours": lead_hours,
        # one side held before history starts: order valid, lead is a minimum
        "lead_is_lower_bound": bool(lead_ok and (sig_cens or coh_cens)),
        "censored": {"signal": sig_cens, "cohort": coh_cens},
        "entry_size": {"signal": sig_size, "cohort": coh_size,
                       "floor": size_floor},
        "entry": {
            "signal_first": {"ts": sig_ts, "source": sig_src},
            "cohort_first": {"ts": coh_ts, "source": coh_src},
        },
        "n_snapshots": len(snaps),
        "n_wallets_total_tracked": len(all_addrs),
        "n_signal": n_sig,
        "n_cohort": len(all_addrs) - n_sig,
        "snapshots": snaps,
        "top_holders": top_list,
        "provenance": {"signal": sig_src, "cohort": coh_src},
        "note": ("signal=delta_likely tracked; cohort=tracked non-signal "
                 "(not the public). first_seen from backfilled entry history "
                 "when available, else snapshot-order (poller start). Lead "
                 "claim valid only when BOTH sides are backfill-sourced."),
    }


def _peak(conn, holder, token_address, token_symbol):
    # balance_history.token = token ADDRESS; snapshots.token = address too.
    if token_address:
        row = conn.execute(
            "SELECT MAX(value_usd) FROM balance_history "
            "WHERE address=? AND token=?", (holder, token_address)).fetchone()
        if row and row[0] is not None and row[0] > 0:
            return row[0]
        row = conn.execute(
            "SELECT MAX(value_usd) FROM balance_snapshots "
            "WHERE address=? AND token=?", (holder, token_address)).fetchone()
        if row and row[0]:
            return row[0]
    row = conn.execute(
        "SELECT MAX(value_usd) FROM balance_snapshots "
        "WHERE address=? AND (token=? OR symbol=?)",
        (holder, token_symbol, (token_symbol or "").upper())).fetchone()
    if row and row[0]:
        return row[0]
    return 0.0


def _usd(v):
    """Exact dollars, or a redaction band passed through as-is."""
    return v if isinstance(v, str) else f"${v:,.0f}"


def _prov_word(p, censored=False):
    # a left-censored backfill entry is "already held when history starts" —
    # it must never be labelled a real entry
    if p == "backfill" and censored:
        return "backfill(censored@history-start)"
    return {"backfill": "backfill(real-entry)",
            "snapshot": "snapshot-order(poller-start)",
            "none": "absent"}[p]


def render(res):
    lines = []
    r = res["resolved"]
    lines.append(f"token   {res['token']}  (symbol={r['symbol']} chain={r['chain']})")
    if res["verdict"] == "NO_TRACKED_EXPOSURE":
        lines.append("verdict NO_TRACKED_EXPOSURE — " + res["note"])
        return "\n".join(lines)

    e = res["entry"]
    cens = res.get("censored") or {}
    v = res["verdict"]
    lead = res.get("lead_hours")
    ge = ">=" if res.get("lead_is_lower_bound") else ""
    tag = ("[LEAD CLAIM: LOWER BOUND]" if res.get("lead_claim") == "lower_bound"
           else "[LEAD CLAIM VALID]")
    if v == "SIGNAL_LEAD":
        lines.append(f"verdict SIGNAL_LEAD — signal entered {ge}{lead}h before the "
                     f"tracked cohort  {tag}")
    elif v == "COHORT_LEAD":
        lines.append(f"verdict COHORT_LEAD — cohort entered {ge}{abs(lead)}h before "
                     f"any signal wallet  {tag}")
    elif v == "CONCURRENT":
        lines.append(f"verdict CONCURRENT — signal and cohort entered within "
                     f"~1h of each other (lead={lead}h, backfill-sourced)")
    elif v == "SIGNAL_ONLY":
        lines.append("verdict SIGNAL_ONLY — only signal (delta-likely) wallets "
                     "hold it; no cohort in tracked set")
    elif v == "COHORT_ONLY":
        lines.append("verdict COHORT_ONLY — tracked wallets hold it, but no "
                     "signal (delta-likely) wallet is in")
    else:  # UNVERIFIED_ENTRY_TIMING
        lines.append("verdict UNVERIFIED_ENTRY_TIMING — entry times not both "
                     "backfill-sourced; NO lead claim possible")
        if all((res.get("censored") or {}).values()):
            lines.append("          (both sides already held when backfill history "
                         "begins — entry order unknown)")
        else:
            lines.append("          (snapshot-order times are poller START, not entry)")

    lines.append(f"entry   signal@{e['signal_first']['ts'][:10] if e['signal_first']['ts'] else '—'}"
                 f" ({_prov_word(e['signal_first']['source'], cens.get('signal'))})  "
                 f"cohort@{e['cohort_first']['ts'][:10] if e['cohort_first']['ts'] else '—'}"
                 f" ({_prov_word(e['cohort_first']['source'], cens.get('cohort'))})")
    sz = res.get("entry_size") or {}
    def _sz_word(s):
        if not s:
            return "—"
        # peak is VALUE (moves with price), not position size
        return (f"{s['wallet']} day1={_usd(s['entry_usd'])} "
                f"(peak value {_usd(s['peak_usd'])})")
    lines.append(f"size    signal: {_sz_word(sz.get('signal'))}  "
                 f"cohort: {_sz_word(sz.get('cohort'))}")
    fl = sz.get("floor")
    if fl is not None:
        if fl == "5k":
            lines.append("floor   entry size >= $5k on BOTH first entries "
                         "[conviction-scale lead]")
        elif fl == "1k":
            lines.append("floor   entry size >= $1k, < $5k on BOTH first "
                         "entries [small-scale lead — not conviction-scale]")
        else:
            amt = fl.split('-', 1)[-1]
            amt = "under $1k" if amt == "1k" else f"${amt}"   # "sub-1k" = redacted
            lines.append(f"floor   smallest first entry {amt} "
                         "— SUB-$1K [small-position lead; "
                         "stated small, not conviction-scale]")
    lines.append(f"n       {res['n_wallets_total_tracked']} tracked wallets "
                 f"({res['n_signal']} signal / {res['n_cohort']} cohort), "
                 f"{res['n_snapshots']} live snapshots")
    if res.get("snapshots"):
        lines.append("live    (deltas only, 3h cadence)")
        for s in res["snapshots"]:
            lines.append(f"  snap {s['snapshot']} ({s['ts'][:16]}) "
                         f"holders={s['n_holders']:<3} sig={s['n_signal']:<2} "
                         f"val={_usd(s['tracked_value_usd'])}")
    lines.append("top    ")
    for t in res["top_holders"]:
        tag = "S" if t["signal"] else " "
        src = {"backfill": "bf", "snapshot": "snap", "none": "--"}[t["entry_source"]]
        et = t["entry_ts"][:10] if t["entry_ts"] else ""
        lines.append(f"  [{tag}] {t['wallet']:<22} {_usd(t['peak_usd']):>11}  "
                     f"entry@{src:<4} {et}")
    lines.append(f"prov    signal-entry={_prov_word(res['provenance']['signal'], cens.get('signal'))}"
                 f"  cohort-entry={_prov_word(res['provenance']['cohort'], cens.get('cohort'))}"
                 f"  lead_claim={res.get('lead_claim', 'none')}")
    lines.append("note    " + res["note"])
    return "\n".join(lines)
