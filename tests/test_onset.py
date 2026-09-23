"""Backtest v2 onset rules — synthetic series, no network, no key."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prelude import onset  # noqa: E402


def _s(days, nf=0.0, mc=1e6, age=30, sym="T"):
    return {d: (nf, mc, age, sym) for d in days}


def test_onset_requires_quiet_prior_and_age():
    loud = _s(onset._days("2026-08-01", "2026-08-09"), nf=6000.0)   # prior sum >> quiet
    loud["2026-08-10"] = (20000.0, 1e6, 30, "L")
    quiet = {"2026-08-10": (20000.0, 1e6, 30, "Q")}
    young = {"2026-08-10": (20000.0, 1e6, 5, "Y")}
    early = {"2026-07-01": (20000.0, 1e6, 30, "E")}                 # before ONSET_FROM
    got = {t: d for t, d, *_ in onset.find_onsets(
        {"L": loud, "Q": quiet, "Y": young, "E": early})}
    assert got == {"Q": "2026-08-10"}


def test_one_onset_per_token_first_qualifying_day():
    s = {"2026-08-10": (20000.0, 1e6, 30, "Q"), "2026-08-30": (50000.0, 1e6, 30, "Q")}
    assert [d for _t, d, *_ in onset.find_onsets({"Q": s})] == ["2026-08-10"]


def test_null_is_quiet_closest_mcap_and_used_once():
    series = {
        "SPK": {"2026-08-10": (20000.0, 1e6, 30, "S")},
        "SPK2": {"2026-08-10": (20000.0, 1e6, 30, "S2")},
        "NEAR": {"2026-08-10": (100.0, 1.1e6, 30, "N")},
        "FAR": {"2026-08-10": (100.0, 9e6, 30, "F")},
        "LOUD": {"2026-08-10": (100.0, 1e6, 30, "X"),
                 "2026-08-14": (15000.0, 1e6, 30, "X")},            # onset in d+7
    }
    spikes = [("SPK", "2026-08-10", 20000.0, 1e6, "S"),
              ("SPK2", "2026-08-10", 20000.0, 1e6, "S2")]
    nulls = {t: n for t, _d, n, *_ in onset.match_nulls(series, spikes)}
    assert nulls["SPK"] == "NEAR" and nulls["SPK2"] == "FAR"


def test_window_hits_amount_increase_only():
    d = "2026-08-10"
    tok = {
        "buyer": {"2026-08-05": (100.0, 50.0)},                       # new in window
        "holder": {"2026-08-01": (100.0, 50.0), "2026-08-05": (100.0, 90.0)},  # price up only
        "adder": {"2026-08-01": (100.0, 50.0), "2026-08-06": (300.0, 2000.0)},
        "late": {"2026-08-10": (100.0, 50.0)},                        # onset day, not pre
    }
    hits = onset.window_hits(tok, d)
    assert hits == {"buyer": "2026-08-05", "adder": "2026-08-06"}
    assert onset.window_hits(tok, d, min_usd=1000.0) == {"adder": "2026-08-06"}


def test_stats_withholds_iqr_below_floor():
    rows = [{"day": "2026-08-10",
             "spike": {"hits": {"w": "2026-08-08"}, "hits_1k": {}},
             "null": {"hits": {}, "hits_1k": {}}}]
    st = onset.stats(rows)
    assert st["n_leads"] == 1 and st["median_lead_hours"] == 48
    assert st["iqr_lead_hours"] is None and st["hits"]["lift"] is None


def test_age_tol_null_excludes_old_tokens():
    series = {"SPK": {"2026-08-10": (20000.0, 1e6, 10, "S")},
              "OLD": {"2026-08-10": (100.0, 1e6, 60, "O")},        # exact mcap, 6x age
              "YNG": {"2026-08-10": (100.0, 3e6, 12, "Y")}}
    sp = [("SPK", "2026-08-10", 20000.0, 1e6, "S")]
    assert onset.match_nulls(series, sp)[0][2] == "OLD"
    assert onset.match_nulls(series, sp, age_tol=1.5)[0][2] == "YNG"


def test_fisher_matches_known_values():
    assert round(onset.fisher_two_sided(18, 38, 4, 52), 4) == 0.0015
    assert round(onset.fisher_two_sided(18, 38, 10, 46), 3) == 0.126
    assert onset.fisher_two_sided(0, 0, 0, 0) is None
