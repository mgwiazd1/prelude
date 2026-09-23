"""Stable wallet pseudonyms for public output (check, recording, README, card).

Roster names AND addresses are private — an SNS name (e.g. `x.sol`) resolves
to an address, so names are treated as addresses. Mapping address -> "Wallet A"
is persisted in data/pseudonyms.json (gitignored) so a letter never moves when
the roster changes; new wallets get the next free letter.
"""
import json
import os
from datetime import datetime

MAP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "pseudonyms.json")


def _label(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return f"Wallet {s}"


class Pseudonyms:
    def __init__(self, path=None):
        self.path = path or MAP_PATH
        try:
            with open(self.path) as f:
                self.map = json.load(f)
        except (OSError, ValueError):
            self.map = {}
        self._dirty = False

    def __call__(self, address):
        if not address:
            return address
        if address not in self.map:
            self.map[address] = _label(len(self.map))
            self._dirty = True
        return self.map[address]

    def save(self):
        if self._dirty:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.map, f, indent=1, sort_keys=True)
            self._dirty = False


def week(ts):
    """'2026-06-25T23:59:59' -> '2026-W26-Thu' (ISO week + weekday).
    Weekday added so two entries in the same week beside "120h before" don't
    read as an error; lead hours already imply the gap."""
    if not ts:
        return ts
    d = datetime.fromisoformat(ts[:10])
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}-{d.strftime('%a')}"


def band(usd):
    if usd is None:
        return None
    return ("<$100" if usd < 100 else "$100-1k" if usd < 1000
            else "$1k-5k" if usd < 5000 else ">=$5k")


def redact_check(res, pseudo, coarsen=True):
    """Return a copy of a check_token result with every wallet name/address
    replaced by its pseudonym. Token addresses are public and kept.

    coarsen (default): a pseudonym + exact day + exact size can be matched to
    on-chain buyers of the token, so per-wallet dates become ISO weeks and
    sizes become bands. Lead hours and the size floor (the claim) are kept."""
    out = json.loads(json.dumps(res))
    for side in ("signal", "cohort"):
        s = (out.get("entry_size") or {}).get(side)
        if s:
            s["wallet"] = pseudo(s.get("address"))
            s["address"] = s["wallet"]
            if coarsen:
                s["entry_usd"], s["peak_usd"] = band(s["entry_usd"]), band(s["peak_usd"])
    for t in out.get("top_holders") or []:
        t["wallet"] = pseudo(t.get("address"))
        t["address"] = t["wallet"]
        if coarsen:
            t["peak_usd"], t["entry_ts"] = band(t["peak_usd"]), week(t["entry_ts"])
    if coarsen:
        es = out.get("entry_size") or {}
        if isinstance(es.get("floor"), str) and es["floor"].startswith("sub-"):
            es["floor"] = "sub-1k"            # exact smallest entry is a fingerprint
        for sn in out.get("snapshots") or []:
            sn["tracked_value_usd"] = band(sn["tracked_value_usd"])
        for side in ("signal_first", "cohort_first"):
            e = (out.get("entry") or {}).get(side)
            if e:
                e["ts"] = week(e["ts"])
    out["redacted"] = "pseudonyms + coarsened (ISO week, USD band)" if coarsen \
        else "pseudonyms"
    return out
