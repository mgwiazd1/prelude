"""Stable wallet pseudonyms for public output (check, recording, README, card).

Roster names AND addresses are private — an SNS name (e.g. `x.sol`) resolves
to an address, so names are treated as addresses. Mapping address -> "Wallet A"
is persisted in data/pseudonyms.json (gitignored) so a letter never moves when
the roster changes; new wallets get the next free letter.
"""
import json
import os

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


def redact_check(res, pseudo):
    """Return a copy of a check_token result with every wallet name/address
    replaced by its pseudonym. Token addresses are public and kept."""
    out = json.loads(json.dumps(res))
    for side in ("signal", "cohort"):
        s = (out.get("entry_size") or {}).get(side)
        if s:
            s["wallet"] = pseudo(s.get("address"))
            s["address"] = s["wallet"]
    for t in out.get("top_holders") or []:
        t["wallet"] = pseudo(t.get("address"))
        t["address"] = t["wallet"]
    out["redacted"] = True
    return out
