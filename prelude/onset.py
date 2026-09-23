"""Backtest v2 — smart-money netflow onsets, hits from stored balance_history.

Replaces the MONI narrative onset source (n=2 in 30d; MONI history is 19-23d
and event-driven, it cannot be widened). DISCLOSED PROXY: an "onset" here is a
smart-money netflow onset from Nansen's historical token screener, not a
narrative onset.

PRE-REGISTERED (fixed 2026-09-23 before any result was computed):
  onset source   POST /api/v1beta1/token-screener/historical, trader_type='sm',
                 timeframe_days=1, to_date=day, Solana, mcap $100k-$25M,
                 age 3-90d (the R2 band). netflow verified reliable on
                 08-01/08-23/09-05 (100% non-null, buy-sell==netflow); data
                 lag pinned at T-1 day (09-22 returned rows on 09-23).
  onset day d    first day with SM netflow >= ONSET_MIN_USD, the prior 7 days'
                 SM netflow summing to < QUIET_MAX_USD, and token_age_days >= 8
                 (>= 7d pre-onset history). One onset per token.
  onset range    ONSET_FROM..ONSET_TO. ONSET_FROM = 07-03: the day before the
                 7d window must be inside balance_history (starts 06-25), or a
                 position held before coverage reads as an "increase".
                 ONSET_TO = 09-15: nulls need a clean +7d forward check.
  window         [d-7, d-1] (7 daily buckets). Daily resolution: minimum
                 resolvable lead = 24h.
  hit            any roster wallet whose token_amount rose day-over-day inside
                 the window (amount, not USD: USD moves with price).
                 Secondary (reported, not the headline): same with day value
                 >= $1k (balance_history USD is soft for micro-caps).
  null           per spike, the token on day d with the closest log market cap
                 that has no day >= ONSET_MIN_USD in [d-7, d+7], age >= 8,
                 not the spike token, each null used once. Same window, same
                 hit rule.
  caps           CAP_SPIKES spikes (seeded random sample if more; seed printed)
                 + one matched null each.
  lead           d - first increase day, x24h. Median always; IQR only when
                 n_hits >= IQR_FLOOR.
"""
import json
import math
import random
import statistics
from datetime import date, datetime, timedelta, timezone

SCREEN_FILTERS = {"market_cap_usd": {"min": 100000, "max": 25000000},
                  "token_age_days": {"min": 3, "max": 90}}
FETCH_FROM = "2026-06-26"   # ONSET_FROM - 7d quiet check
FETCH_TO = "2026-09-22"     # T-1 lag horizon (pinned 2026-09-23)
ONSET_FROM = "2026-07-03"
ONSET_TO = "2026-09-15"
ONSET_MIN_USD = 10_000.0
QUIET_MAX_USD = 5_000.0
MIN_AGE_DAYS = 8
WINDOW_DAYS = 7
HIT_SECONDARY_USD = 1000.0
CAP_SPIKES = 100
SEED = 20260923
IQR_FLOOR = 5
MAX_FETCH_CALLS = 200       # hard stop for the enumeration spend (5c/call)
MAX_PAGES_PER_DAY = 5       # <=1,000 tokens/day; band days run 52-200 rows


def _days(a, b):
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [(d0 + timedelta(n)).isoformat() for n in range((d1 - d0).days + 1)]


def _shift(day, n):
    return (date.fromisoformat(day) + timedelta(n)).isoformat()


def fetch_plan(conn, day_from=FETCH_FROM, day_to=FETCH_TO):
    done = {r[0] for r in conn.execute(
        "SELECT day FROM sm_screener_days WHERE status='done'")}
    return [d for d in _days(day_from, day_to) if d not in done]


def fetch_daily(client, conn, day_from=FETCH_FROM, day_to=FETCH_TO,
                max_calls=MAX_FETCH_CALLS, verbose=True):
    """Fetch one screener call per day (paging as needed). Resume-safe."""
    todo = fetch_plan(conn, day_from, day_to)
    if verbose:
        print(f"  pre-spend: {len(todo)} days to fetch ({day_from}..{day_to}), "
              f"est ~{len(todo)}-{2 * len(todo)} calls at 5c, "
              f"hard stop {max_calls} calls, credits before = "
              f"{client.credits_remaining}")
    calls = 0
    for day in todo:
        page, rows, credits = 1, [], 0
        while True:
            if calls >= max_calls:
                print(f"  HARD STOP at {calls} calls — {day} left pending")
                conn.commit()
                return calls
            r = client.screener_historical(
                day, 1, filters=SCREEN_FILTERS, page=page,
                context_note=f"onset sm-netflow {day} p{page}")
            calls += 1
            credits += int(client.last_cost or 5)
            data = r.get("data") or []
            seen = {x["token_address"] for x in rows}
            fresh = [x for x in data if x["token_address"] not in seen]
            rows.extend(fresh)
            # is_last_page alone is NOT a stop condition: 2026-09-21 returned
            # is_last_page=false for 113 pages (565c burned). Stop on an empty
            # page, a page with no new tokens, or MAX_PAGES_PER_DAY.
            if (r.get("pagination") or {}).get("is_last_page", True) \
                    or not fresh or page >= MAX_PAGES_PER_DAY:
                break
            page += 1
        conn.executemany("""INSERT OR REPLACE INTO sm_netflow_daily
            (day, token, token_symbol, chain, netflow, buy_volume, sell_volume,
             market_cap_usd, token_age_days) VALUES (?,?,?,?,?,?,?,?,?)""",
            [(day, x["token_address"], x.get("token_symbol"),
              x.get("chain", "solana"), x.get("netflow"), x.get("buy_volume"),
              x.get("sell_volume"), x.get("market_cap_usd"),
              x.get("token_age_days")) for x in rows])
        conn.execute("""INSERT OR REPLACE INTO sm_screener_days
            (day, pages, rows, credits, filters, status, fetched_ts)
            VALUES (?,?,?,?,?,'done',?)""",
            (day, page, len(rows), credits, json.dumps(SCREEN_FILTERS),
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        conn.commit()
        if verbose:
            print(f"  {day}: {len(rows)} tokens, {page} page(s), {credits}c")
    return calls


# --- selection (stored data only, zero API cost) ---------------------------

def _netflow_by_token(conn):
    out = {}
    for day, tok, sym, nf, mc, age in conn.execute(
            "SELECT day, token, token_symbol, netflow, market_cap_usd, "
            "token_age_days FROM sm_netflow_daily"):
        out.setdefault(tok, {})[day] = (nf or 0.0, mc, age, sym)
    return out


def find_onsets(series):
    """Pre-registered onset rule. Returns [(token, day, netflow, mcap, sym)]."""
    onsets = []
    for tok, s in series.items():
        for day in sorted(s):
            if not (ONSET_FROM <= day <= ONSET_TO):
                continue
            nf, mc, age, sym = s[day]
            if nf < ONSET_MIN_USD or (age or 0) < MIN_AGE_DAYS:
                continue
            prior = sum(s.get(_shift(day, -k), (0.0,))[0]
                        for k in range(1, WINDOW_DAYS + 1))
            if prior >= QUIET_MAX_USD:
                continue
            onsets.append((tok, day, nf, mc, sym))
            break                      # one onset per token
    return onsets


def match_nulls(series, spikes, age_tol=None):
    """One null per spike: closest log-mcap token on day d, quiet in d±7.
    age_tol (POST-HOC sensitivity, not pre-registered): also require the
    null's age within x/÷ age_tol of the spike's — added 2026-09-23 after the
    first run showed spike tokens median 21d old vs nulls 49.5d."""
    used, out = set(), []
    for tok, day, _nf, mc, _sym in spikes:
        best = None
        s_age = series[tok][day][2] if tok in series and day in series[tok] else None
        for cand, s in series.items():
            if cand == tok or (cand, day) in used or day not in s:
                continue
            c_nf, c_mc, c_age, c_sym = s[day]
            if (c_age or 0) < MIN_AGE_DAYS or not c_mc or not mc:
                continue
            if age_tol and (not s_age or abs(math.log(c_age) - math.log(s_age))
                            > math.log(age_tol)):
                continue
            if any(s.get(_shift(day, k), (0.0,))[0] >= ONSET_MIN_USD
                   for k in range(-WINDOW_DAYS, WINDOW_DAYS + 1)):
                continue
            dist = (abs(math.log(c_mc) - math.log(mc)), cand)
            if best is None or dist < best[0]:
                best = (dist, cand, c_mc, c_sym)
        if best:
            used.add((best[1], day))
            out.append((tok, day, best[1], best[2], best[3]))
    return out


# --- hits from balance_history ----------------------------------------------

def _amounts(conn, tokens):
    """{token: {wallet: {day: (amount, value_usd)}}} for the given tokens."""
    out = {}
    q = ",".join("?" * len(tokens))
    for a, tok, ts, amt, val in conn.execute(
            f"SELECT address, token, block_timestamp, token_amount, value_usd "
            f"FROM balance_history WHERE token IN ({q})", list(tokens)):
        out.setdefault(tok, {}).setdefault(a, {})[ts[:10]] = (amt or 0.0, val or 0.0)
    return out


def _amt_on(rows, day):
    """Carry-forward amount (0 before the wallet's first row for the token)."""
    prior = [d for d in rows if d <= day]
    return rows[max(prior)][0] if prior else 0.0


def window_hits(amounts_tok, day, min_usd=0.0):
    """Wallets whose amount rose day-over-day in [day-7, day-1].
    Returns {wallet: first_increase_day}."""
    hits = {}
    for wallet, rows in (amounts_tok or {}).items():
        for k in range(WINDOW_DAYS, 0, -1):
            x = _shift(day, -k)
            if x in rows and rows[x][0] > _amt_on(rows, _shift(x, -1)) \
                    and rows[x][1] >= min_usd:
                hits[wallet] = x
                break
    return hits


def run(conn, roster=None, verbose=True, age_tol=None):
    series = _netflow_by_token(conn)
    n_days = conn.execute("SELECT COUNT(*) FROM sm_screener_days "
                          "WHERE status='done'").fetchone()[0]
    onsets = find_onsets(series)
    rng = random.Random(SEED)
    spikes = sorted(onsets, key=lambda o: (o[1], o[0]))
    if len(spikes) > CAP_SPIKES:
        spikes = sorted(rng.sample(spikes, CAP_SPIKES), key=lambda o: (o[1], o[0]))
    nulls = match_nulls(series, spikes, age_tol=age_tol)
    by_spike = {(t, d): (nt, nmc, nsym) for t, d, nt, nmc, nsym in nulls}
    pairs = [s for s in spikes if (s[0], s[1]) in by_spike]
    if verbose:
        print(f"  n  stage0 screener days fetched ........ {n_days}")
        print(f"  n  stage1 candidate tokens (any day) ... {len(series)}")
        print(f"  n  stage2 anchored onsets (rule) ....... {len(onsets)}")
        print(f"  n  stage3 selected spikes (cap {CAP_SPIKES}, seed {SEED}) "
              f"{len(spikes)}")
        print(f"  n  stage4 spikes with a matched null ... {len(pairs)}")
    toks = {s[0] for s in pairs} | {by_spike[(s[0], s[1])][0] for s in pairs}
    amts = _amounts(conn, toks) if toks else {}
    rows = []
    for tok, day, nf, mc, sym in pairs:
        nt, nmc, nsym = by_spike[(tok, day)]
        sh = window_hits(amts.get(tok), day)
        nh = window_hits(amts.get(nt), day)
        rows.append({
            "day": day, "spike": {"token": tok, "symbol": sym, "sm_netflow": nf,
                                  "mcap": mc, "hits": sh,
                                  "hits_1k": window_hits(amts.get(tok), day,
                                                         HIT_SECONDARY_USD)},
            "null": {"token": nt, "symbol": nsym, "mcap": nmc, "hits": nh,
                     "hits_1k": window_hits(amts.get(nt), day,
                                            HIT_SECONDARY_USD)}})
    return rows, stats(rows)


def fisher_two_sided(a, b, c, d):
    """Exact two-sided Fisher p for [[a, b], [c, d]] (spike hit/miss vs null)."""
    n, r1, c1 = a + b + c + d, a + b, a + c
    if not n or not c1 or c1 == n:
        return None
    def pr(x):
        return math.comb(r1, x) * math.comb(n - r1, c1 - x) / math.comb(n, c1)
    p0 = pr(a)
    return sum(pr(x) for x in range(max(0, c1 - (n - r1)), min(r1, c1) + 1)
               if pr(x) <= p0 * (1 + 1e-9))


def _r4(x):
    return None if x is None else round(x, 4)


def stats(rows):
    n = len(rows)
    def rate(side, key):
        h = sum(1 for r in rows if r[side][key])
        return h, (h / n if n else None)
    out = {"n_pairs": n}
    for key in ("hits", "hits_1k"):
        sh, sr = rate("spike", key)
        nh, nr = rate("null", key)
        out[key] = {"spike_hits": sh, "null_hits": nh,
                    "hit_rate_spike": round(sr, 3) if sr is not None else None,
                    "hit_rate_null": round(nr, 3) if nr is not None else None,
                    "lift": round(sr / nr, 2) if nr else None,
                    "p_fisher": _r4(fisher_two_sided(sh, n - sh, nh, n - nh))}
    leads = [(date.fromisoformat(r["day"]) -
              date.fromisoformat(min(r["spike"]["hits"].values()))).days * 24
             for r in rows if r["spike"]["hits"]]
    out["n_leads"] = len(leads)
    out["median_lead_hours"] = statistics.median(leads) if leads else None
    out["iqr_lead_hours"] = None
    if len(leads) >= IQR_FLOOR:
        q = statistics.quantiles(leads, n=4, method="inclusive")
        out["iqr_lead_hours"] = [q[0], q[2]]
    out["iqr_floor"] = IQR_FLOOR
    return out


# --- probe-then-size hypothesis (TEST ONLY — no claims until measured) -------
# MG 2026-09-23: "do small signal first-entries get followed by scale-ups and
# by cohort entry more often than in null windows?" Measured on token AMOUNT:
# JLY's "$678 -> $1,929" was price (amount flat, exited day 3), not scaling.
PROBE_MAX_USD = 1000.0
PROBE_FOLLOW_DAYS = 14
SCALE_MULT = 2.0


def first_entries(conn, pop_of, max_usd=PROBE_MAX_USD, last_day=None):
    """Small first entries: (wallet, token, day, amount, usd, pop).
    Entry day must be >= 06-26 (prior day covered => truly new) and leave
    PROBE_FOLLOW_DAYS of follow-up before last_day."""
    last_day = last_day or _shift(FETCH_TO, -PROBE_FOLLOW_DAYS)
    out = []
    for a, tok, ts, amt, val in conn.execute(
            "SELECT address, token, MIN(block_timestamp), token_amount, value_usd "
            "FROM balance_history WHERE token_amount > 0 "
            "GROUP BY address, token"):
        day = ts[:10]
        if "2026-06-26" <= day <= last_day and 0 < (val or 0) < max_usd:
            out.append((a, tok, day, amt, val, pop_of(a)))
    return out


def probe_outcomes(conn, entries, pop_of):
    """Per entry: scaled (amount >= SCALE_MULT x entry within follow window)
    and other_followed (a wallet of the OTHER population first-enters the
    token within the follow window)."""
    toks = {e[1] for e in entries}
    amts = _amounts(conn, toks) if toks else {}
    firsts = {}
    for tok, wallets in amts.items():
        for w, rows in wallets.items():
            held = [d for d, (amt, _v) in rows.items() if amt > 0]
            if held:
                firsts.setdefault(tok, {})[w] = min(held)
    res = []
    for a, tok, day, amt, val, pop in entries:
        end = _shift(day, PROBE_FOLLOW_DAYS)
        rows = amts.get(tok, {}).get(a, {})
        scaled = any(day < d <= end and v[0] >= SCALE_MULT * amt
                     for d, v in rows.items())
        other = any(pop_of(w) != pop and day < fd <= end
                    for w, fd in firsts.get(tok, {}).items())
        res.append({"wallet": a, "token": tok, "day": day, "usd": val,
                    "pop": pop, "scaled": scaled, "other_followed": other})
    return res


def probe_report(conn, rows_bt, pop_of):
    """Compare small SIGNAL first-entries inside spike vs null windows, plus
    an unconditional signal-vs-cohort baseline. Every rate carries its n."""
    ents = first_entries(conn, pop_of)
    outs = probe_outcomes(conn, ents, pop_of)
    spike_w, null_w = set(), set()
    for r in rows_bt:
        for k in range(1, WINDOW_DAYS + 1):
            spike_w.add((r["spike"]["token"], _shift(r["day"], -k)))
            null_w.add((r["null"]["token"], _shift(r["day"], -k)))
    def summ(xs):
        n = len(xs)
        return {"n": n,
                "scaled": sum(x["scaled"] for x in xs),
                "other_followed": sum(x["other_followed"] for x in xs),
                "scaled_rate": round(sum(x["scaled"] for x in xs) / n, 3) if n else None,
                "other_followed_rate": round(sum(x["other_followed"] for x in xs) / n, 3)
                if n else None}
    sig = [o for o in outs if o["pop"] == "signal"]
    return {
        "definition": f"first entry < ${PROBE_MAX_USD:,.0f}; scaled = amount >= "
                      f"{SCALE_MULT}x within {PROBE_FOLLOW_DAYS}d; other_followed = "
                      f"other population first-enters within {PROBE_FOLLOW_DAYS}d",
        "signal_in_spike_windows": summ([o for o in sig if (o["token"], o["day"]) in spike_w]),
        "signal_in_null_windows": summ([o for o in sig if (o["token"], o["day"]) in null_w]),
        "signal_all": summ(sig),
        "cohort_all_baseline": summ([o for o in outs if o["pop"] == "cohort"]),
    }


def leave_out_top(rows, k=2):
    """POST-HOC concentration check: drop the k wallets with the most spike
    hits (chosen from the data, not by name) from BOTH sides and recompute.
    States concentration only — per-wallet n is too small to infer skill."""
    from collections import Counter
    top = [a for a, _ in Counter(a for r in rows for a in r["spike"]["hits"]).most_common(k)]
    trimmed = [{"day": r["day"],
                "spike": {**r["spike"], "hits": {a: d for a, d in r["spike"]["hits"].items() if a not in top},
                          "hits_1k": {}},
                "null": {**r["null"], "hits": {a: d for a, d in r["null"]["hits"].items() if a not in top},
                         "hits_1k": {}}} for r in rows]
    st = stats(trimmed)
    return {"k": k, "top_wallet_spike_hits": [n for _, n in Counter(
        a for r in rows for a in r["spike"]["hits"]).most_common(k)], "hits": st["hits"]}
