"""check <token> fixture tests — temp DB, fixture roster, no network, no key.

The load-bearing test is test_snapshot_order_never_claims_lead: with entry
times available ONLY from balance_snapshots (poller start), check must
downgrade to UNVERIFIED_ENTRY_TIMING, never SIGNAL_LEAD/COHORT_LEAD.
Real first_seen comes from balance_history (the backfill).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prelude import check, db  # noqa: E402

S = "sigWalletAddr"      # delta_likely=True (signal)
C1 = "cohWallet1"        # delta_likely unset (cohort)


def _seed(tmp_path):
    dbfile = str(tmp_path / "chk.db")
    conn = db.connect(dbfile)
    roster = {"roster": [
        {"address": S, "name": "Signal", "chain": "solana",
         "status": "active", "delta_likely": True},
        {"address": C1, "name": "Cohort1", "chain": "solana", "status": "active"},
    ]}
    rpath = str(tmp_path / "roster.json")
    json.dump(roster, open(rpath, "w"))

    def snap(sid, addr, sym, val, ts):
        conn.execute(
            "INSERT OR REPLACE INTO balance_snapshots "
            "(snapshot_id, ts, address, chain, token, symbol, value_usd, "
            "price_usd, token_amount) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, ts, addr, "solana", sym, sym, val, None, None))

    def hist(addr, tok, ts, val):
        conn.execute(
            "INSERT OR REPLACE INTO balance_history "
            "(address, chain, token, token_symbol, block_timestamp, "
            "token_amount, value_usd) VALUES (?,?,?,?,?,?,?)",
            (addr, "solana", tok, tok, ts, 100.0, val))

    # SIGLD: signal entered 2026-08-01, cohort 2026-08-09 (8d later)
    hist(S, "SIGLD", "2026-08-01T04:00:00Z", 5000.0)
    hist(C1, "SIGLD", "2026-08-09T09:30:00Z", 2000.0)
    # COHLD: cohort entered 2026-07-05, signal 2026-07-12
    hist(C1, "COHLD", "2026-07-05T12:00:00Z", 3000.0)
    hist(S, "COHLD", "2026-07-12T15:00:00Z", 4000.0)
    # CONC: within 30 min of each other
    hist(S, "CONC", "2026-08-15T10:00:00Z", 1000.0)
    hist(C1, "CONC", "2026-08-15T10:30:00Z", 1500.0)
    # SONLY: only the signal wallet, backfilled
    hist(S, "SONLY", "2026-08-20T00:00:00Z", 9000.0)
    # CONLY: only a cohort (non-delta_likely) holder, backfilled
    hist(C1, "CONLY", "2026-08-22T00:00:00Z", 12000.0)
    # SNAPONLY: NO balance_history rows at all — snapshot-only exposure
    # (this is the poller-start trap: 3 same-day snapshots)
    for sid, ts in ((1, "2026-09-22T19:43:00Z"),
                    (2, "2026-09-22T21:07:00Z"),
                    (3, "2026-09-22T22:00:00Z")):
        snap(sid, C1, "SNAPONLY", 500.0, ts)
        if sid >= 2:
            snap(sid, S, "SNAPONLY", 700.0, ts)

    conn.execute("INSERT INTO snapshots (id, ts, wallets) VALUES "
                 "(1,'2026-09-22T19:43:00Z',2), (2,'2026-09-22T21:07:00Z',2), "
                 "(3,'2026-09-22T22:00:00Z',2)")
    conn.commit()
    return conn, rpath


def test_signal_lead_backfilled(tmp_path):
    conn, rpath = _seed(tmp_path)
    res = check.check_token(conn, "SIGLD", roster_path=rpath)
    assert res["verdict"] == "SIGNAL_LEAD"
    assert res["lead_claim_valid"] is True
    assert res["lead_hours"] == 197.5, f"8d+5h30m = 197.5h, got {res['lead_hours']}"
    assert res["provenance"] == {"signal": "backfill", "cohort": "backfill"}
    assert "LEAD CLAIM VALID" in check.render(res)


def test_cohort_lead_backfilled(tmp_path):
    conn, rpath = _seed(tmp_path)
    res = check.check_token(conn, "COHLD", roster_path=rpath)
    assert res["verdict"] == "COHORT_LEAD"
    assert res["lead_claim_valid"] is True
    assert res["lead_hours"] == -171.0  # signal entered 7d+3h LATER
    assert "LEAD CLAIM VALID" in check.render(res)


def test_concurrent_backfilled(tmp_path):
    conn, rpath = _seed(tmp_path)
    res = check.check_token(conn, "CONC", roster_path=rpath)
    assert res["verdict"] == "CONCURRENT"
    assert res["lead_claim_valid"] is True
    assert res["lead_hours"] == 0.5


def test_snapshot_order_never_claims_lead(tmp_path):
    """The correctness gate: with 3 same-day snapshots and NO backfill,
    'signal appears at snapshot 2, cohort at snapshot 1' must NOT render as a
    lead — poller start is not entry."""
    conn, rpath = _seed(tmp_path)
    res = check.check_token(conn, "SNAPONLY", roster_path=rpath)
    assert res["verdict"] == "UNVERIFIED_ENTRY_TIMING", \
        f"snapshot-order rendered as lead: {res['verdict']}"
    assert res["lead_claim_valid"] is False
    assert res["lead_hours"] is None
    assert res["provenance"]["signal"] == "snapshot"
    assert res["provenance"]["cohort"] == "snapshot"
    text = check.render(res)
    assert "NO lead claim possible" in text
    assert "poller START" in text
    assert "signal-entry=snapshot-order(poller-start)" in text


def test_gate_fix_cohort_only_surfaces(tmp_path):
    """Gate = tracked holder, not delta_likely. A non-signal tracked holder
    must surface as an exposure (COHORT_ONLY), never a null verdict."""
    conn, rpath = _seed(tmp_path)
    res = check.check_token(conn, "CONLY", roster_path=rpath)
    assert res["verdict"] == "COHORT_ONLY"
    assert res["n_wallets_total_tracked"] == 1
    assert res["top_holders"][0]["address"] == C1
    assert res["top_holders"][0]["entry_source"] == "backfill"
    assert "COHORT_ONLY" in check.render(res)


def test_signal_only(tmp_path):
    conn, rpath = _seed(tmp_path)
    res = check.check_token(conn, "SONLY", roster_path=rpath)
    assert res["verdict"] == "SIGNAL_ONLY"
    assert res["n_signal"] == 1 and res["n_cohort"] == 0
    assert res["lead_claim_valid"] is False


def test_unknown_token_clean_exit(tmp_path):
    conn, rpath = _seed(tmp_path)
    res = check.check_token(conn, "NOPE", roster_path=rpath)
    assert res["verdict"] == "NO_TRACKED_EXPOSURE"
    assert res["n_wallets_total_tracked"] == 0
    assert "NO_TRACKED_EXPOSURE" in check.render(res)


def _hist(conn, addr, tok, ts, val):
    conn.execute(
        "INSERT OR REPLACE INTO balance_history "
        "(address, chain, token, token_symbol, block_timestamp, "
        "token_amount, value_usd) VALUES (?,?,?,?,?,?,?)",
        (addr, "solana", tok, tok, ts, 100.0, val))
    conn.commit()


def test_entry_size_printed_on_every_lead_verdict(tmp_path):
    conn, rpath = _seed(tmp_path)
    for tok in ("SIGLD", "COHLD", "CONC"):
        res = check.check_token(conn, tok, roster_path=rpath)
        out = check.render(res)
        assert "size    signal:" in out and "floor   " in out, tok
    res = check.check_token(conn, "SIGLD", roster_path=rpath)
    assert res["entry_size"]["signal"]["entry_usd"] == 5000.0
    assert res["entry_size"]["cohort"]["entry_usd"] == 2000.0
    assert res["entry_size"]["floor"] == "1k"   # min side sets the floor


def test_size_floor_sub_1k_stated_small(tmp_path):
    conn, rpath = _seed(tmp_path)
    _hist(conn, S, "SMALL", "2026-08-01T00:00:00Z", 678.0)
    _hist(conn, S, "SMALL", "2026-08-03T00:00:00Z", 1929.0)
    _hist(conn, C1, "SMALL", "2026-08-02T00:00:00Z", 4431.0)
    res = check.check_token(conn, "SMALL", roster_path=rpath)
    assert res["verdict"] == "SIGNAL_LEAD"
    assert res["entry_size"]["signal"]["peak_usd"] == 1929.0
    assert res["entry_size"]["floor"] == "sub-678"
    assert "SUB-$1K" in check.render(res)
    assert "conviction-scale lead]" not in check.render(res)


def test_size_floor_dust_entry_never_conviction_scale(tmp_path):
    # regression: a sub-half-cent entry rounds to 0.0; it must still set the floor
    conn, rpath = _seed(tmp_path)
    _hist(conn, S, "DUST", "2026-08-01T00:00:00Z", 0.004)
    _hist(conn, C1, "DUST", "2026-08-05T00:00:00Z", 9000.0)
    res = check.check_token(conn, "DUST", roster_path=rpath)
    assert res["verdict"] == "SIGNAL_LEAD"
    assert res["entry_size"]["floor"] != "5k"
    assert "SUB-$1K" in check.render(res)


def test_size_floor_5k_conviction_scale(tmp_path):
    conn, rpath = _seed(tmp_path)
    _hist(conn, S, "BIG", "2026-08-01T00:00:00Z", 6000.0)
    _hist(conn, C1, "BIG", "2026-08-05T00:00:00Z", 7000.0)
    res = check.check_token(conn, "BIG", roster_path=rpath)
    assert res["entry_size"]["floor"] == "5k"
    assert "[conviction-scale lead]" in check.render(res)


def test_redact_removes_every_roster_name_and_address(tmp_path):
    from prelude import redact
    conn, rpath = _seed(tmp_path)
    for tok in ("SIGLD", "COHLD", "CONLY", "SNAPONLY"):
        res = check.check_token(conn, tok, roster_path=rpath)
        pseudo = redact.Pseudonyms(str(tmp_path / "ps.json"))
        red = redact.redact_check(res, pseudo)
        pseudo.save()
        blob = check.render(red) + json.dumps(red)
        for secret in (S, C1, "Signal", "Cohort1"):
            assert secret not in blob, (tok, secret)
        assert "Wallet " in blob
    # stable across instances: same address, same letter
    p1 = redact.Pseudonyms(str(tmp_path / "ps.json"))
    assert p1(S) == redact.Pseudonyms(str(tmp_path / "ps.json"))(S)


def _cov(conn, *addrs):
    for a in addrs:
        conn.execute("INSERT OR REPLACE INTO backfill_runs (address, range_from, range_to,"
                     " status) VALUES (?, '2026-06-25', '2026-09-23', 'done')", (a,))
    conn.commit()


def test_both_sides_censored_is_not_concurrent(tmp_path):
    conn, rpath = _seed(tmp_path)
    _cov(conn, S, C1)
    _hist(conn, S, "OLDHELD", "2026-06-25T23:59:59", 500.0)
    _hist(conn, C1, "OLDHELD", "2026-06-25T23:59:59", 700.0)
    res = check.check_token(conn, "OLDHELD", roster_path=rpath)
    assert res["verdict"] == "UNVERIFIED_ENTRY_TIMING"
    assert res["lead_claim_valid"] is False
    assert "entry order unknown" in check.render(res)


def test_one_side_censored_lead_is_lower_bound(tmp_path):
    conn, rpath = _seed(tmp_path)
    _cov(conn, S, C1)
    _hist(conn, C1, "HALF", "2026-06-25T23:59:59", 500.0)
    _hist(conn, S, "HALF", "2026-08-18T23:59:59", 700.0)
    res = check.check_token(conn, "HALF", roster_path=rpath)
    assert res["verdict"] == "COHORT_LEAD" and res["lead_is_lower_bound"] is True
    assert "cohort entered >=" in check.render(res)
    # without coverage info (no backfill_runs row) nothing is censored
    res2 = check.check_token(conn, "SIGLD", roster_path=rpath)
    assert res2["lead_is_lower_bound"] is False


def test_redact_coarsens_dates_and_sizes(tmp_path):
    import re
    from prelude import redact
    conn, rpath = _seed(tmp_path)
    _hist(conn, S, "SMALL", "2026-08-01T00:00:00Z", 678.0)
    _hist(conn, C1, "SMALL", "2026-08-02T00:00:00Z", 4431.0)
    res = check.check_token(conn, "SMALL", roster_path=rpath)
    red = redact.redact_check(res, redact.Pseudonyms(str(tmp_path / "p.json")))
    out = check.render(red)
    assert not re.search(r"2026-\d\d-\d\d", out), out      # no exact days
    assert "$678" not in out and "$4,431" not in out       # no exact sizes
    assert "2026-W31" in out and "$100-1k" in out and "$1k-5k" in out
    assert red["lead_hours"] == res["lead_hours"]           # the claim is kept
    raw = redact.redact_check(res, redact.Pseudonyms(str(tmp_path / "p.json")),
                              coarsen=False)
    assert "$678" in check.render(raw)


def test_censored_entry_never_renders_as_real_entry(tmp_path):
    from prelude import redact
    conn, rpath = _seed(tmp_path)
    _cov(conn, S, C1)
    _hist(conn, C1, "CENS", "2026-06-25T23:59:59", 500.0)   # held at history start
    _hist(conn, S, "CENS", "2026-08-18T23:59:59", 700.0)
    res = check.check_token(conn, "CENS", roster_path=rpath)
    assert res["lead_claim"] == "lower_bound" and res["censored"]["cohort"] is True
    for r in (res, redact.redact_check(res, redact.Pseudonyms(str(tmp_path / "p.json")))):
        out = check.render(r)
        coh = [l for l in out.splitlines() if l.startswith(("entry", "prov"))]
        assert all("cohort@" not in l or "censored@history-start" in l.split("cohort@")[1]
                   for l in coh if l.startswith("entry"))
        assert "cohort-entry=backfill(censored@history-start)" in out
        assert "cohort-entry=backfill(real-entry)" not in out
        assert "lead_claim=lower_bound" in out and "LEAD CLAIM VALID" not in out
        assert "[LEAD CLAIM: LOWER BOUND]" in out
    # uncensored claim keeps the exact label
    ok = check.render(check.check_token(conn, "SIGLD", roster_path=rpath))
    assert "lead_claim=valid" in ok and "censored" not in ok


def test_tied_earliest_entry_is_deterministic_largest(tmp_path):
    import json as _j
    C2 = "cohWallet2"
    conn, rpath = _seed(tmp_path)
    r = _j.load(open(rpath))
    r["roster"].append({"address": C2, "name": "Cohort2", "chain": "solana", "status": "active"})
    _j.dump(r, open(rpath, "w"))
    _hist(conn, S, "TIE", "2026-08-10T23:59:59", 50.0)
    _hist(conn, C1, "TIE", "2026-08-03T23:59:59", 1500.0)
    _hist(conn, C2, "TIE", "2026-08-03T23:59:59", 43000.0)     # same day, larger
    res = check.check_token(conn, "TIE", roster_path=rpath)
    assert res["entry_size"]["cohort"]["address"] == C2
    assert res["entry_size"]["cohort"]["entry_usd"] == 43000.0
