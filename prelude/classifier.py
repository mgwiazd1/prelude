"""Four-way classifier (spec §D.3, v1.5) + composite score (§D.5, v1.1).

stealth_lead   : t_hi <= t_onset - 12h AND narrative quiet at t_hi
chase          : t_lo >= t_confirm
concurrent     : between onset and confirm (reported, never claimed as lead)
distribution   : exit/trim AND z_narr > 2 AND (exchange_net_flow_usd > 0 OR DIV < -1.5)
                 NULL fallback: exchange_net_flow_usd None → DIV < -1.5 alone.
                 NEVER use exchange_wallet_count.
retro          : unmatched deltas re-classified if a narrative onsets within 72h of t_hi.
"""
from datetime import datetime, timedelta, timezone

from . import narrative

STEALTH_BUFFER = timedelta(hours=12)
LEAD_CONFIRMED_FLOOR = timedelta(hours=3)  # v1.6: minimum resolvable at 3h cadence
RETRO_WINDOW = timedelta(hours=72)


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def classify(event, onsets=None, exchange_netflow_usd=None, divergence=None):
    """event: diff.py event dict (+ address/chain). Returns {cls, lead_hours, narrative_slug}."""
    onsets = onsets if onsets is not None else narrative.load()
    t_lo, t_hi = _parse(event["t_lo"]), _parse(event["t_hi"])
    onset = narrative.onset_for(event["token"], event.get("chain", ""), onsets)

    cls, lead_hours = None, None
    if onset is not None:
        t_onset = _parse(onset["t_onset"])
        t_confirm = _parse(onset["t_confirm"]) if onset.get("t_confirm") else None
        if event["kind"] in ("exit", "trim") and _is_distribution(event, onset, exchange_netflow_usd, divergence):
            cls = "distribution"
        elif t_hi <= t_onset - STEALTH_BUFFER:
            if narrative.quiet_at(onset, event["t_hi"], onsets):
                # ALERTING class — deliberate conservatism: >=12h, 4+ snapshot gaps at 3h
                cls = "stealth_lead"
            else:
                # pre-onset but narrative not quiet — a lead is NOT claimed
                cls = "unclaimed_pre_onset"
            lead_hours = (t_onset - t_hi).total_seconds() / 3600.0
        elif t_hi > t_onset - LEAD_CONFIRMED_FLOOR:
            cls = None  # onset or after — fall through to chase/concurrent
        else:
            # v1.6 lead_confirmed: 3–12h pre-onset band. Reported in the backtest
            # table + per-wallet score ONLY — never in the live alerting class.
            # Tx-pinned in backtest (exact entry ts); the null control bounds false positives.
            cls = "lead_confirmed"
            lead_hours = (t_onset - t_hi).total_seconds() / 3600.0
        if cls is None:
            if t_confirm is not None and t_lo >= t_confirm:
                cls = "chase"
            else:
                cls = "concurrent"  # onset→confirm (or straddling) — reported, never claimed
            lead_hours = None

    if cls is None:
        # retro: unmatched delta, narrative onsets within 72h of t_hi
        for o in onsets:
            if o["token"] == event["token"] and o.get("chain", "") == event.get("chain", ""):
                t_onset = _parse(o["t_onset"])
                if timedelta(0) <= t_onset - t_hi <= RETRO_WINDOW:
                    cls = "retro_unmatched"
    return {"cls": cls or "unclassified", "lead_hours": lead_hours,
            "narrative_slug": onset["slug"] if onset else None}


def _is_distribution(event, onset, exchange_netflow_usd, divergence):
    z_narr = onset.get("z_narr") or 0.0
    if z_narr <= 2.0:
        return False
    if exchange_netflow_usd is None:
        # v1.5 null fallback: divergence-only, never exchange_wallet_count
        return divergence is not None and divergence < -1.5
    return exchange_netflow_usd > 0 or (divergence is not None and divergence < -1.5)


def composite_score(event, cls, lead_hours, tier_weight, z_delta_usd,
                    divergence=None, z_narr=None):
    """Spec §D.5. DIV class-signed: +DIV rewards stealth, -DIV rewards distribution."""
    if cls == "distribution":
        z_narr_term = min(max(z_narr - 2, 0.0), 3.0) / 3.0 if z_narr is not None else 0.0
        div_term = (0.1 * (-divergence / 3.0)) if divergence is not None else 0.0
        return 0.4 * min(abs(z_delta_usd), 3.0) / 3.0 + 0.3 * z_narr_term + 0.2 * tier_weight + div_term
    div_term = (0.1 * (divergence / 3.0)) if divergence is not None else 0.0
    lead_term = 0.3 * min(lead_hours, 72.0) / 72.0 if lead_hours is not None else 0.0
    return 0.4 * min(abs(z_delta_usd), 3.0) / 3.0 + lead_term + 0.2 * tier_weight + div_term
