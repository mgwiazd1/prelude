-- Prelude: migrations/001_init.sql
-- Local state DB. Metering lives in the SHARED ~/remi-intelligence/remi_intelligence.db
-- (api_usage table) — never create api_usage here.

CREATE TABLE IF NOT EXISTS wallets (
  address      TEXT NOT NULL,
  chain        TEXT NOT NULL,
  tier         TEXT NOT NULL,           -- operator | smart_money | related | cold
  tier_weight  REAL NOT NULL,
  source       TEXT,
  labels_json  TEXT,
  status       TEXT NOT NULL DEFAULT 'active',
  last_delta_at TEXT,
  PRIMARY KEY (address, chain)
);

CREATE TABLE IF NOT EXISTS snapshots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- snapshot_id: monotonic, one per pass
  ts           TEXT NOT NULL,                      -- display column (wall clock at pass start)
  wallets      INTEGER,                            -- active wallets targeted
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
  ts           TEXT NOT NULL,                      -- display column; GROUP/DIFF on snapshot_id
  address      TEXT NOT NULL,
  chain        TEXT NOT NULL,
  token        TEXT NOT NULL,
  symbol       TEXT,
  value_usd    REAL NOT NULL DEFAULT 0,
  price_usd    REAL,
  token_amount REAL,
  UNIQUE (snapshot_id, address, chain, token)
);
CREATE INDEX IF NOT EXISTS idx_snap_sid ON balance_snapshots(snapshot_id);

CREATE TABLE IF NOT EXISTS delta_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  address     TEXT NOT NULL,
  chain       TEXT NOT NULL,
  token       TEXT NOT NULL,
  symbol      TEXT,
  kind        TEXT NOT NULL,            -- enter | add | trim | exit
  delta_usd   REAL NOT NULL,
  prev_usd    REAL,
  new_usd     REAL,
  t_lo        TEXT NOT NULL,            -- prev snapshot ts
  t_hi        TEXT NOT NULL,            -- detect snapshot ts
  snapshot_lo INTEGER,
  snapshot_hi INTEGER
);
CREATE INDEX IF NOT EXISTS idx_delta_token ON delta_events(token, t_hi);

CREATE TABLE IF NOT EXISTS narrative_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL,
  token       TEXT NOT NULL,
  chain       TEXT NOT NULL,
  t_onset     TEXT NOT NULL,
  t_confirm   TEXT,
  z_narr      REAL,
  rank        INTEGER,
  moni_change REAL,
  UNIQUE (slug, t_onset)
);

CREATE TABLE IF NOT EXISTS scores (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  delta_id      INTEGER NOT NULL REFERENCES delta_events(id),
  narrative_id  INTEGER REFERENCES narrative_events(id),
  cls           TEXT NOT NULL,          -- stealth_lead | chase | concurrent | distribution | unclassified
  score         REAL,
  lead_hours    REAL,                   -- band midpoint (stealth only; NULL for others)
  divergence    REAL,                   -- DIV: clamp(z_roster_net24h - z_crowd_net24h, -3, 3)
  z_delta_usd   REAL,
  z_narr        REAL,
  exchange_netflow_usd REAL,            -- NULL allowed → divergence-only fallback
  score_flip    INTEGER NOT NULL DEFAULT 0,  -- cohort re-classified within 7d
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scores_delta ON scores(delta_id);

CREATE TABLE IF NOT EXISTS credits_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL DEFAULT (datetime('now')),
  credits_remaining INTEGER NOT NULL,   -- from X-Nansen-Credits-Remaining header
  caller          TEXT
);

CREATE TABLE IF NOT EXISTS roster_suggestions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL DEFAULT (datetime('now')),
  source_wallet   TEXT NOT NULL,        -- stealth_lead wallet that triggered
  candidate       TEXT NOT NULL,
  candidate_chain TEXT,
  status          TEXT NOT NULL DEFAULT 'suggested',  -- suggested | approved | rejected
  approved_at     TEXT
);

CREATE TABLE IF NOT EXISTS backtest_windows (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  token        TEXT NOT NULL,
  chain        TEXT NOT NULL,
  symbol       TEXT,
  mcap_usd     REAL,
  kind         TEXT NOT NULL,           -- spike | null
  w_from       TEXT NOT NULL,           -- window start (6h windows, v1.6)
  w_to         TEXT NOT NULL,           -- onset t for spikes
  match_token  TEXT,                    -- null control: matched token (same or mcap-matched)
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
  hits         TEXT,                    -- JSON [{address, label, net_usd}]
  net_buy_usd  REAL,                    -- max roster net-buy in window
  n_smart_rows INTEGER,
  lead_hours   REAL,                    -- snapshot-diff entry lead (tx-pinned; NULL = no entry detected)
  executed_ts  TEXT,
  error        TEXT,
  created_ts   TEXT NOT NULL,
  roster_size  INTEGER,                 -- wallet set size at selection (for audit)
  UNIQUE (token, kind, w_from, w_to)
);
CREATE INDEX IF NOT EXISTS idx_btwin_kind ON backtest_windows(kind, status);
