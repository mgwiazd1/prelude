"""CLI: python -m prelude <snapshot|diff|classify|score|receipt|demo|backtest*|site|replay-stub>"""
import json
import sys


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "help"

    if cmd == "demo":
        from .demo import run
        run()
    elif cmd == "snapshot":
        try:
            import os  # main() imports os per-branch, so it is function-local
            from . import poller
            live = "--live-view" in argv
            if live:
                r = poller.load_roster()
                print(f"[live] roster file: {os.path.relpath(poller.ROSTER_PATH)} "
                      f"({len(r)} wallets) · caller=prelude · every call metered")
            poller.snapshot_all(live_view=live)
        except RuntimeError as e:
            print(f"prelude: {e}")
            sys.exit(1)
    elif cmd in ("backtest", "backtest-select", "backtest-run",
                 "backtest-stats", "backtest-clear", "backtest-artifact"):
        from . import db, backtest
        conn = db.connect()
        if cmd == "backtest-select":
            import os
            ch = os.environ.get("PRELUDE_BT_CHAIN", "solana")
            n = backtest.select_spikes(conn, chain=ch)
            print(f"selected {n} new windows (chain={ch})")
        elif cmd == "backtest-clear":
            cur = conn.execute("DELETE FROM backtest_windows")
            conn.commit()
            print(f"cleared {cur.rowcount} windows")
        elif cmd == "backtest-run":
            import os
            lim = os.environ.get("PRELUDE_BT_LIMIT", "")
            n, credits = backtest.run_windows(
                conn, limit=int(lim) if lim else None)
            print(f"executed {n} windows, credits={credits}")
        elif cmd == "backtest-stats":
            print(json.dumps(backtest.stats(conn), indent=2))
        elif cmd == "backtest-artifact":
            out, path = backtest.artifact(conn)
            print(path)
            print(json.dumps(out, indent=2))
        else:
            print(__doc__)
    elif cmd in ("onset-plan", "onset-fetch", "onset-run"):
        # backtest v2: SM-netflow onsets (screener), hits from balance_history
        import os
        from . import db, onset, check as _check
        conn = db.connect()
        conn.row_factory = None
        if cmd == "onset-plan":
            todo = onset.fetch_plan(conn)
            print(f"onset-plan: {len(todo)} days pending "
                  f"({onset.FETCH_FROM}..{onset.FETCH_TO}), "
                  f"~{len(todo)}-{2 * len(todo)} calls x 5c; no calls made")
        elif cmd == "onset-fetch":
            from . import nansen_client
            client = nansen_client.NansenClient(caller="prelude_backtest")
            n = onset.fetch_daily(client, conn)
            print(f"onset-fetch: {n} calls, credits={client.credits_remaining}")
        else:
            roster = _check._load_roster(os.environ.get("PRELUDE_ROSTER_PATH"))
            def pop_of(a):
                return "signal" if (roster.get(a) or {}).get("delta_likely") is True \
                    else "cohort"
            rows, st = onset.run(conn)
            print("  -- post-hoc sensitivity: age-matched null (x/÷1.5) --")
            rows_age, st_age = onset.run(conn, age_tol=1.5)
            probe = onset.probe_report(conn, rows, pop_of)
            conc = {"pre_registered": onset.leave_out_top(rows),
                    "age_matched": onset.leave_out_top(rows_age)}
            out = {"stats": st, "sensitivity_age_matched_posthoc": st_age,
                   "concentration_leave_out_top2_posthoc": conc,
                   "probe_hypothesis": probe, "pairs": rows,
                   "pairs_age_matched": rows_age,
                   "params": {k: getattr(onset, k) for k in (
                       "ONSET_FROM", "ONSET_TO", "ONSET_MIN_USD", "QUIET_MAX_USD",
                       "MIN_AGE_DAYS", "WINDOW_DAYS", "CAP_SPIKES", "SEED",
                       "IQR_FLOOR", "SCREEN_FILTERS")}}
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "out", "onset_backtest.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(out, f, indent=2)
            # public copy: every wallet address -> stable pseudonym (tokens are public)
            from . import redact
            pseudo = redact.Pseudonyms()
            def _red(o):
                if isinstance(o, dict):
                    return {(pseudo(k) if k in roster else k): _red(v) for k, v in o.items()}
                if isinstance(o, list):
                    return [_red(v) for v in o]
                return pseudo(o) if isinstance(o, str) and o in roster else o
            # per-pair hit COUNTS only: wallet x token x day lists would let
            # anyone intersect on-chain buyers and de-anonymise the roster
            def _counts(pairs):
                return [{**p, "spike": {**p["spike"], "hits": len(p["spike"]["hits"]),
                                        "hits_1k": len(p["spike"]["hits_1k"])},
                         "null": {**p["null"], "hits": len(p["null"]["hits"]),
                                  "hits_1k": len(p["null"]["hits_1k"])}} for p in pairs]
            pub = _red({**out, "pairs": _counts(out["pairs"]),
                        "pairs_age_matched": _counts(out["pairs_age_matched"])})
            pub["redacted"] = ("wallet identities removed; per-pair hit counts "
                               "only (wallet x token x day would de-anonymise)")
            ppath = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "results", "onset_backtest.public.json")
            os.makedirs(os.path.dirname(ppath), exist_ok=True)
            with open(ppath, "w") as f:
                json.dump(pub, f, indent=2)
            pseudo.save()
            blob = json.dumps(pub)
            leaks = [a for a in roster if a and a in blob] + \
                [w["name"] for w in roster.values() if len(w.get("name") or "") > 3
                 and w["name"] in blob]
            if leaks:
                os.remove(ppath)
                raise SystemExit(f"REDACTION FAILED ({len(leaks)} leaks) — public file removed")
            print(ppath)
            print(json.dumps({"stats": st, "sensitivity_age_matched_posthoc": st_age,
                              "concentration_leave_out_top2_posthoc": conc,
                              "probe_hypothesis": probe}, indent=2))
            print(path)
    elif cmd == "site":
        # static results page for GitHub Pages: reads ONLY the committed public results; no key, no API calls
        import os, subprocess
        from . import site
        site.build()
        scanner = os.environ.get("PRELUDE_LEAK_SCAN",
                                 os.path.expanduser("~/remi-intelligence/scripts/prelude_leak_scan.py"))
        if os.path.exists(scanner):
            r = subprocess.run([scanner, "dir", os.path.dirname(site.OUT)], capture_output=True, text=True)
            print(r.stdout.strip() or r.stderr.strip())
            if r.returncode != 0:
                os.remove(site.OUT)
                raise SystemExit("site: leak scan failed on docs/ — page removed")
        else:
            print("site: leak scanner not available here; the pre-commit hook scans the staged page")
        print(site.OUT)
    elif cmd == "backtest-table":
        # reads the COMMITTED public results: runs on a clean clone, no key
        import os
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "results", "onset_backtest.public.json")) as f:
            bt = json.load(f)
        pre, age = bt["stats"], bt["sensitivity_age_matched_posthoc"]
        conc = bt["concentration_leave_out_top2_posthoc"]["age_matched"]["hits"]
        n = pre["n_pairs"]
        def row(label, h, mark=""):
            p = h["p_fisher"]
            sig = "" if p is None or p < 0.05 else "  (not significant)"
            return (f"{mark}{label:<46} {h['spike_hits']:>2}/{n}  {h['null_hits']:>2}/{n}"
                    f"  {h['lift']:>4}x  p={p:.3f}{sig}")
        print(f"Onset backtest - Solana, SM-netflow onsets Jul 3-Sep 15 2026, n={n} pairs")
        print(f"  {'null control':<46} spike   null   lift")
        b0, b1 = ("\033[1;33m", "\033[0m") if sys.stdout.isatty() else ("", "")
        print(b0 + row("age-matched  [added after seeing results]", age["hits"], "> ") + b1)
        print(row("market-cap matched  [pre-registered]", pre["hits"], "  "))
        print(row("age-matched, top-2 wallets removed", conc, "  "))
        print(f"  median lead {pre['median_lead_hours']:.0f}h (IQR "
              f"{pre['iqr_lead_hours'][0]:.0f}-{pre['iqr_lead_hours'][1]:.0f}h, "
              f"n={pre['n_leads']}) - bounded by the 7d window, not timing precision")
    elif cmd == "receipt-backtest":
        import os
        from . import receipt
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "out", "onset_backtest.json")) as f:
            bt = json.load(f)
        # committed asset (README embeds it above the fold), not out/
        print(receipt.render_confound(bt, os.path.join(base, "docs", "receipt_backtest.png"),
                                      repo_url=os.environ.get("PRELUDE_REPO_URL")))
    elif cmd == "check":
        if len(argv) < 2:
            print("usage: python -m prelude check <symbol-or-address> [--json] [--no-redact]")
            return
        from . import db, check, redact
        conn = db.connect()
        res = check.check_token(conn, argv[1])
        if "--no-redact" not in argv:
            # default ON: roster names/addresses are private (SNS names
            # resolve to addresses); stable pseudonyms, mapping gitignored
            pseudo = redact.Pseudonyms()
            res = redact.redact_check(res, pseudo)
            pseudo.save()
        if "--json" in argv:
            print(json.dumps(res, indent=2))
        else:
            print(check.render(res))
    elif cmd == "backfill":
        import os as _os
        from datetime import datetime, timedelta, timezone
        from . import db, backfill, nansen_client
        days = int(_os.environ.get("PRELUDE_BACKFILL_DAYS", "90"))
        limit = _os.environ.get("PRELUDE_BACKFILL_LIMIT", "")
        to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        frm = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        client = nansen_client.NansenClient(caller="prelude")
        conn = db.connect()
        print(f"backfill: {days}d window ({frm}..{to}), 1c/page")
        s = backfill.backfill_all(client, conn,
                                  limit=int(limit) if limit else None,
                                  range_from=frm, range_to=to)
        print(f"done: {s['done_now']} wallets now "
              f"({s['already_done']} already), {s['rows']} rows, "
              f"{s['pages']} pages, {s['failed']} failed, "
              f"credits used={s['credits']}, remaining={s['credits_remaining']}")
        if s["failed"]:
            print("FAILED wallets (see backfill_runs): resumable on re-run")
            sys.exit(1)
    elif cmd == "backfill-status":
        from . import db
        conn = db.connect()
        rows = conn.execute(
            "SELECT address, range_from, pages, rows, credits, status "
            "FROM backfill_runs ORDER BY address").fetchall()
        n = len(rows)
        done = sum(1 for r in rows if r["status"] == "done")
        tot_c = sum(r["credits"] or 0 for r in rows)
        tot_r = sum(r["rows"] or 0 for r in rows)
        print(f"balance_history: {conn.execute('SELECT COUNT(*) FROM balance_history').fetchone()[0]} rows")
        print(f"backfill_runs: {n} total, {done} done, credits={tot_c}, rows={tot_r}")
        for r in rows:
            print(f"  {r['address'][:10]} {r['range_from']} {r['status']:>6} "
                  f"pages={r['pages']} rows={r['rows']} c={r['credits']}")
    elif cmd == "replay-stub":
        print("replay.py — beta-history animation (historical-address-balance, beta). "
              "Schema pinned in fixtures on day 1 per spec §F.")
    elif cmd in ("diff", "classify", "score", "receipt"):
        print(f"{cmd}: part of the demo pipeline (fixture mode) — run `make demo` for the full pass.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
