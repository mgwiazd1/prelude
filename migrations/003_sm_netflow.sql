-- Backtest onset source (item 2, 2026-09-23): daily smart-money netflow per
-- token from POST /api/v1beta1/token-screener/historical (trader_type='sm',
-- timeframe_days=1, to_date=day). A token absent on a day = not in the
-- screened band that day (no SM activity OR outside the mcap/age filter).
CREATE TABLE IF NOT EXISTS sm_netflow_daily (
  day             TEXT NOT NULL,          -- YYYY-MM-DD (screener to_date)
  token           TEXT NOT NULL,          -- token ADDRESS
  token_symbol    TEXT,
  chain           TEXT NOT NULL DEFAULT 'solana',
  netflow         REAL,                   -- SM buy - sell, USD, that day
  buy_volume      REAL,
  sell_volume     REAL,
  market_cap_usd  REAL,
  token_age_days  INTEGER,
  PRIMARY KEY (day, token)
);
CREATE INDEX IF NOT EXISTS idx_smnf_token ON sm_netflow_daily(token, day);

-- one row per fetched day (resume-safe; a day is re-fetched unless 'done')
CREATE TABLE IF NOT EXISTS sm_screener_days (
  day         TEXT PRIMARY KEY,
  pages       INTEGER NOT NULL DEFAULT 0,
  rows        INTEGER NOT NULL DEFAULT 0,
  credits     INTEGER NOT NULL DEFAULT 0,
  filters     TEXT,                       -- JSON of the request filters
  status      TEXT NOT NULL DEFAULT 'pending',
  fetched_ts  TEXT
);
