"""Budget gates (spec §C, v1.6 rebase). Poller caller only; bursty callers separate.

- PRELUDE_MAX_DAY=1500 / PRELUDE_MAX_7D=8000 on caller='prelude'
- Credit floors (106K balance): <5,000 → flow-intelligence to 12h cadence;
  <2,000 → balances-only
- Hard-lock last 1,000 credits (demo reserve) until Sep 26.
"""
import os

from . import api_usage

MAX_DAY = int(os.environ.get("PRELUDE_MAX_DAY", "1500"))
MAX_7D = int(os.environ.get("PRELUDE_MAX_7D", "8000"))
DEMO_RESERVE_CREDITS = 1000
FLOOR_FI_12H = 5_000
FLOOR_BALANCES_ONLY = 2_000


def gate_ok(caller="prelude"):
    today, week = api_usage.usage_counts(caller=caller, since_days=7)
    return today < MAX_DAY and week < MAX_7D


def gate_status(caller="prelude"):
    today, week = api_usage.usage_counts(caller=caller, since_days=7)
    return {"today": today, "day_cap": MAX_DAY, "week": week, "week_cap": MAX_7D,
            "ok": today < MAX_DAY and week < MAX_7D}


def cadence_mode(credits_remaining):
    """Spec §C v1.6 floors, applied to the raw balance:
    <2,000 → balances-only; <5,000 → fi-12h. The 1,000c demo reserve is
    enforced separately at spend-time (usable_credits)."""
    if credits_remaining is None:
        return "full"
    if credits_remaining < FLOOR_BALANCES_ONLY:
        return "balances_only"
    if credits_remaining < FLOOR_FI_12H:
        return "fi_12h"
    return "full"


def usable_credits(credits_remaining):
    """Credits above the demo reserve."""
    if credits_remaining is None:
        return None
    return max(0, credits_remaining - DEMO_RESERVE_CREDITS)
