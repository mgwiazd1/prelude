"""Nansen API client (sync, httpx). Every call metered to the shared api_usage.

Caller names (gate-scoped): prelude (poller), prelude_backtest, prelude_pin.
Credit cost table (§G RESULTS): current-balance 1c, netflow 5c, flow-intelligence
1c, flows 1c, who-bought-sold 1c, address-transactions 1c, related-wallets 1c.
"""
import os
import time
import fcntl

import httpx

from . import api_usage

BASE = "https://api.nansen.ai"
CREDIT_COST = {
    "/api/v1/profiler/address/current-balance": 1,
    "/api/v1/profiler/address/historical-balances": 1,
    "/api/v1/tgm/netflow": 5,
    "/api/v1/tgm/flow-intelligence": 1,
    "/api/v1/tgm/flows": 1,
    "/api/v1/tgm/who-bought-sold": 1,
    "/api/v1/profiler/address-transactions": 1,
    "/api/v1/profiler/address/related-wallets": 1,
    "/api/v1beta1/tgm/historical-who-bought-sold": 1,
    # backtest onset source (docs: backtesting-data/historical-token-screener,
    # token-god-mode/price-ohlcv); verify against last_cost on first live call
    "/api/v1beta1/token-screener/historical": 5,
    "/api/v1/tgm/token-ohlcv": 1,
    # labels measured ~127c/call via credits delta 2026-09-23 (8 calls, 508c)
    "/api/v1/profiler/address/labels": 127,
}
MIN_SPACING_S = 2.5
_last_call = 0.0

# --- cross-process global queue (v1.6 §G 9c) ---
# The 2.5s spacing above is per-process (module state). Separate systemd timers
# firing concurrently would each keep their own _last_call, so peak req/s is the
# SUM. A cross-process fcntl lock on a shared file makes the queue GLOBAL:
# every Prelude job (balance 3h, flow-intel 6h, daily flows, backtest, tx-pin)
# serializes through ONE queue, so overlapping timers can never exceed
# ~1 call per MIN_SPACING_S regardless of how many jobs run at once.
_LOCK_PATH = os.path.join(
    os.path.dirname(os.environ.get("PRELUDE_DB", os.getcwd())), "prelude_nansen.lock")
_STATE_PATH = _LOCK_PATH + ".state"


def _read_last():
    try:
        with open(_STATE_PATH) as f:
            return float(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0.0


def _write_last(t):
    try:
        with open(_STATE_PATH, "w") as f:
            f.write(repr(t))
    except OSError:
        pass


class _GlobalQueue:
    """Holds a cross-process lock + spacing across all Nansen calls.

    The lock serializes so only one job issues at a time; the shared state
    file records the real wall-clock of the last call, so a job that acquires
    the lock after another job releases it still honors MIN_SPACING_S from
    that other job's last request. Net effect: one global queue, ~1 call /
    MIN_SPACING_S across ALL concurrent Prelude jobs.
    """

    def __enter__(self):
        self._f = open(_LOCK_PATH, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)  # block until no other job holds it
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._f, fcntl.LOCK_UN)
        finally:
            self._f.close()
        return False

    def wait_spacing(self):
        """Block until MIN_SPACING_S has elapsed since the last call (global)."""
        while True:
            elapsed = time.monotonic() - _read_last()
            if elapsed >= MIN_SPACING_S:
                return
            time.sleep(MIN_SPACING_S - elapsed)

    def stamp(self):
        now = time.monotonic()
        _write_last(now)
        global _last_call
        _last_call = now


def _chain(chain):
    return "bnb" if chain in ("bsc", "bnb") else chain


class NansenClient:
    def __init__(self, key=None, caller="prelude"):
        self.key = key or os.environ.get("PRELUDE_NANSEN_KEY")
        if not self.key:
            raise RuntimeError(
                "set PRELUDE_NANSEN_KEY (see .env.example) — live mode requires a key; "
                "use `make demo` for fixture mode"
            )
        self.caller = caller
        self.client = httpx.Client(timeout=30)
        self.credits_remaining = None
        self.last_cost = None  # X-Nansen-Credits-Cost of the last response

    def _post(self, endpoint, payload, _retry=1):
        note = str((payload or {}).get("context_note") or "")
        clean = {k: v for k, v in payload.items() if k != "context_note"}
        with _GlobalQueue() as q:
            q.wait_spacing()  # global: block until MIN_SPACING_S since the LAST call by ANY job
            t0 = time.monotonic()
            try:
                body = ({**clean, "chain": _chain(clean["chain"])}
                        if "chain" in clean else clean)  # screener takes `chains`
                r = self.client.post(BASE + endpoint, headers={"apiKey": str(self.key)},
                                     json=body)
            except httpx.TransportError as e:
                # timeout/connection error raises BEFORE any status exists, so the
                # 5xx retry below never saw it and one slow wallet killed the whole
                # pass (snapshot_id=6, 2026-09-23 07:00Z). Meter (status NULL — the
                # server may have charged), 1 retry w/ 30s backoff, then RuntimeError
                # so the poller dead-letters this wallet instead of aborting.
                q.stamp()
                api_usage.log_api_call(
                    "nansen", endpoint, self.caller, http_status=None,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    units=CREDIT_COST.get(endpoint, 1), unit_kind="credits",
                    context=(note + " " if note else "") + type(e).__name__)
                r = e
            else:
                q.stamp()  # record this call's wall-clock for the next job (global)
            dur = int((time.monotonic() - t0) * 1000)
        if isinstance(r, httpx.TransportError):
            # retry OUTSIDE the queue: _GlobalQueue holds an flock, re-entering
            # from inside it on a new fd would block on our own lock
            if _retry > 0:
                time.sleep(30)
                return self._post(endpoint, payload, _retry - 1)
            raise RuntimeError(f"{type(r).__name__} {endpoint} after retry") from r
        self.credits_remaining = int(r.headers.get("x-nansen-credits-remaining") or 0) or None
        self.last_cost = r.headers.get("x-nansen-credits-cost")
        api_usage.log_api_call(
            "nansen", endpoint, self.caller, http_status=r.status_code,
            duration_ms=dur, units=CREDIT_COST.get(endpoint, 1), unit_kind="credits",
            context=note or None,
        )
        if r.status_code >= 500 and _retry > 0:
            # failure table: 5xx/timeout → 1 retry, 30s backoff (attempt already metered)
            time.sleep(30)
            return self._post(endpoint, payload, _retry - 1)
        if r.status_code == 422:
            # Nansen rejects unknown/invalid fields with 422 — metered above, then
            # raised for the dead-letter path (failure table: 422 = per-object)
            raise RuntimeError(f"422 {endpoint}: {r.text[:200]}")
        if r.status_code == 401:
            raise RuntimeError(f"401 {endpoint} — key invalid or endpoint retired")
        if r.status_code == 402:
            raise RuntimeError(f"402 {endpoint} — rate/credit limit; global pause")
        r.raise_for_status()
        return r.json()

    # --- verified endpoints (§G) ---
    def current_balance(self, address, chain, context_note=None):
        return self._post("/api/v1/profiler/address/current-balance",
                          {"address": address, "chain": chain, "context_note": context_note})

    def netflow(self, chain, token_addresses, timeframe="1d", context_note=None):
        return self._post("/api/v1/tgm/netflow",
                          {"chain": chain, "token_address": token_addresses,
                           "timeframe": timeframe, "context_note": context_note})

    def flow_intelligence(self, chain, token_address, timeframe="1d", context_note=None):
        return self._post("/api/v1/tgm/flow-intelligence",
                          {"chain": chain, "token_address": token_address,
                           "timeframe": timeframe, "context_note": context_note})

    def who_bought_sold(self, chain, token_address, address_label="smart_money",
                        date_from=None, date_to=None, context_note=None):
        # v1.6 backtest linchpin. Live-verified param shape: `date` range
        # {from, to} "YYYY-MM-DD" (NOT a timeframe string — 422 "missing date"
        # caught during backtest scaffolding, 2026-09-22). Window width =
        # timing precision: 6h backtest windows use from==to same day.
        return self._post("/api/v1/tgm/who-bought-sold",
                          {"chain": chain, "token_address": token_address,
                           "address_label": address_label,
                           "date": {"from": date_from, "to": date_to},
                           "context_note": context_note})

    def historical_who_bought_sold(self, chain, token_address, date_from,
                                   date_to, context_note=None):
        # v1beta1 — backtest endpoint (verified live 2026-09-22, 1c/call):
        # per-address buy/sell USD aggregates over date_range with
        # address_label resolved AT date_to (temporally correct — the F5
        # look-ahead fix). NO per-tx timestamps: window query is a
        # hit/miss; lead time comes from our snapshot diffs.
        return self._post("/api/v1beta1/tgm/historical-who-bought-sold",
                          {"chain": chain, "token_address": token_address,
                           "date_range": {"from": date_from, "to": date_to},
                           "pagination": {"page": 1, "recordsPerPage": 1000},
                           "context_note": context_note})

    def address_transactions(self, address, chain, token_address=None, context_note=None):
        payload = {"address": address, "chain": chain, "context_note": context_note}
        if token_address:
            payload["token_address"] = token_address
        return self._post("/api/v1/profiler/address-transactions", payload)

    def related_wallets(self, chain, address, context_note=None, records_per_page=50):
        # path confirmed live (operator-intel crosswallet.py): profiler/address/related-wallets
        return self._post("/api/v1/profiler/address/related-wallets",
                          {"address": address, "chain": chain,
                           "pagination": {"page": 1, "recordsPerPage": records_per_page},
                           "context_note": context_note})

    def historical_balances(self, address, chain, date_from, date_to,
                            context_note=None):
        """Full per-wallet balance history (NOT top-N), daily resolution.

        Standard endpoint /api/v1/profiler/address/historical-balances — 1c per
        page, full lookback (90d+ verified live 2026-09-22). Pages until the
        last page and returns (all_rows, n_pages). Rows:
        {block_timestamp, chain, token_address, token_amount, token_symbol,
         value_usd}.
        """
        rows, page, is_last = [], 1, False
        while not is_last:
            r = self._post("/api/v1/profiler/address/historical-balances",
                           {"address": address, "chain": chain,
                            "date": {"from": date_from, "to": date_to},
                            "pagination": {"page": page, "per_page": 1000},
                            "context_note": context_note})
            rows.extend((r.get("data") or []))
            is_last = (r.get("pagination") or {}).get("is_last_page", True)
            page += 1
            if page > 50:
                break
        return rows, page - 1

    def token_ohlcv(self, chain, token_addresses, date_from, date_to,
                    timeframe="1d", context_note=None):
        """Batch OHLCV (max 10 tokens/call). Shape from docs 2026-09-23:
        `token_addresses` + `date:{from,to}` — NOT flat date_from (that is the
        v1beta1 historical-token-ohlcv contract; 422 unknown_field here)."""
        return self._post("/api/v1/tgm/token-ohlcv",
                          {"chain": chain, "token_addresses": list(token_addresses)[:10],
                           "date": {"from": date_from, "to": date_to},
                           "timeframe": timeframe, "context_note": context_note})

    def screener_historical(self, to_date, timeframe_days, chains=("solana",),
                            trader_type="sm", filters=None, page=1, per_page=1000,
                            context_note=None):
        """Point-in-time token screener anchored at to_date. netflow is
        trader_type-scoped: the default ("all") is NOT smart-money netflow.
        per_page=1000 (max): the endpoint IGNORES `page` (echoes page 1 with
        is_last_page=false forever, verified 2026-09-23), so one page must
        hold the whole day."""
        return self._post("/api/v1beta1/token-screener/historical",
                          {"to_date": to_date, "timeframe_days": timeframe_days,
                           "chains": list(chains), "trader_type": trader_type,
                           "filters": filters or {},
                           "pagination": {"page": page, "per_page": per_page},
                           "context_note": context_note})
