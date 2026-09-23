"""Narrative onsets. Day-1: local JSON fixture. Later: MONI velocity join.

Interface the rest of the pipeline depends on:
  onset_for(token, chain) -> dict | None   (t_onset, t_confirm, z_narr, rank, moni_change)
  quiet_at(onset, ts) -> bool              (narrative quiet at ts: rank outside top-50 AND |moni_change| < 1 sigma)
"""
import json
import os

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "fixtures")


def load(path=None):
    with open(path or os.path.join(FIXTURES, "narrative.json")) as f:
        return json.load(f)["onsets"]


def onset_for(token, chain, onsets=None):
    onsets = onsets if onsets is not None else load()
    for o in onsets:
        if o["token"] == token and o["chain"] == chain:
            return o
    return None


def quiet_at(onset, ts_iso, onsets=None):
    """Narrative quiet at ts (before t_onset): rank outside top-50 and |moni_change| < 1 sigma.

    The fixture carries the quiet-at-prev-snapshot verdict explicitly (demo mode);
    in live mode this is computed from the MONI velocity history.
    """
    if "quiet_at_prev_snapshot" in onset:
        from datetime import datetime, timezone
        t_hi = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        t_onset = datetime.fromisoformat(onset["t_onset"].replace("Z", "+00:00"))
        # only the pre-onset snapshots are relevant
        return onset["quiet_at_prev_snapshot"] and t_hi <= t_onset
    rank = onset.get("rank") or 999
    moni = abs(onset.get("moni_change") or 0)
    return rank > 50 and moni < 1.0
