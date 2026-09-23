-- 002: balance_history — backfilled per-(wallet,token) entry history.
-- Source: POST /api/v1/profiler/address/historical-balances (1c/page, full
-- history not top-N, DAILY resolution, verified 90d+ lookback 2026-09-22).
--
-- This is the CORRECT source for first_seen (when the wallet actually entered
-- the token). balance_snapshots first-appearance is poller start time, NOT
-- entry — a check verdict may only claim a lead from THIS table.
CREATE TABLE IF NOT EXISTS balance_history (
  address         TEXT NOT NULL,
  chain           TEXT NOT NULL,
  token           TEXT NOT NULL,
  token_symbol    TEXT,
  block_timestamp TEXT NOT NULL,
  token_amount    REAL,
  value_usd       REAL NOT NULL DEFAULT 0,
  UNIQUE (address, chain, token, block_timestamp)
);
CREATE INDEX IF NOT EXISTS idx_bh_token_ts ON balance_history(token, block_timestamp);
CREATE INDEX IF NOT EXISTS idx_bh_addr ON balance_history(address);

-- per-wallet backfill bookkeeping (idempotent re-runs, resume on failure)
CREATE TABLE IF NOT EXISTS backfill_runs (
  address      TEXT NOT NULL,
  range_from   TEXT NOT NULL,
  range_to     TEXT NOT NULL,
  pages        INTEGER NOT NULL DEFAULT 0,
  rows         INTEGER NOT NULL DEFAULT 0,
  credits      INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'pending',
  finished_ts  TEXT,
  PRIMARY KEY (address, range_from)
);
