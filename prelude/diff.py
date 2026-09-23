"""Snapshot diffing → delta events.

Thresholds (spec §D.1): position ≥ $5k AND (|Δ| ≥ $2k OR 20%).
Kinds: enter (prev 0), add, trim, exit (new 0). Event carries [t_lo, t_hi].
"""

MIN_POSITION = 5_000.0
MIN_ABS_DELTA = 2_000.0
MIN_PCT_DELTA = 0.20


def balances_of(snapshot):
    """Accept {'ts':..., 'balances':{addr:[rows]}} or bare {addr:[rows]}."""
    if isinstance(snapshot, dict) and "balances" in snapshot:
        return snapshot["balances"]
    return snapshot


def ts_of(snapshot):
    if isinstance(snapshot, dict) and "ts" in snapshot:
        return snapshot["ts"]
    raise ValueError("snapshot missing ts")


def diff_snapshots(lo, hi):
    """lo/hi: snapshot dicts (see balances_of). Returns list of delta event dicts."""
    events = []
    lo_bal, hi_bal = balances_of(lo), balances_of(hi)
    t_lo, t_hi = ts_of(lo), ts_of(hi)
    for addr in sorted(set(lo_bal) | set(hi_bal)):
        a_lo = {r["token"]: r for r in lo_bal.get(addr, [])}
        a_hi = {r["token"]: r for r in hi_bal.get(addr, [])}
        for token in sorted(set(a_lo) | set(a_hi)):
            row_lo = a_lo.get(token)
            row_hi = a_hi.get(token)
            prev = row_lo["value_usd"] if row_lo else 0.0
            new = row_hi["value_usd"] if row_hi else 0.0
            delta = new - prev
            if max(prev, new) < MIN_POSITION:
                continue
            if abs(delta) < MIN_ABS_DELTA and abs(delta) < MIN_PCT_DELTA * max(prev, new, 1e-9):
                continue
            if prev == 0:
                kind = "enter"
            elif new == 0:
                kind = "exit"
            elif delta > 0:
                kind = "add"
            else:
                kind = "trim"
            row = row_hi or row_lo
            row = row or {}
            events.append({
                "address": addr,
                "chain": row.get("chain", ""),
                "token": token,
                "symbol": row.get("symbol"),
                "kind": kind,
                "delta_usd": delta,
                "prev_usd": prev,
                "new_usd": new,
                "t_lo": t_lo,
                "t_hi": t_hi,
            })
    return events
