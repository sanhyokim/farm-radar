-- Farm Radar のデータベース（SQLite）。SPEC 6章 + M1の追加指示。
-- 大きな整数（uint128/uint160 など）は桁あふれを避けるため TEXT で保存する。

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS venues (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  chain TEXT NOT NULL,
  type TEXT,
  audited INTEGER,
  launch_date TEXT,
  verified INTEGER NOT NULL DEFAULT 0,
  source_url TEXT,
  checked_at TEXT
);

CREATE TABLE IF NOT EXISTS pools (
  id TEXT PRIMARY KEY,                -- "<venue_id>:<pool address>"
  venue_id TEXT NOT NULL REFERENCES venues(id),
  address TEXT NOT NULL,
  token0 TEXT NOT NULL,
  token1 TEXT NOT NULL,
  fee_tier INTEGER,
  tick_spacing INTEGER,
  is_stock_pair INTEGER,
  has_perp INTEGER,
  gauge_address TEXT,
  created_block INTEGER,              -- ファクトリーの作成イベントのブロック
  discovered_at TEXT NOT NULL
);

-- 1回の収集（15分ごと）の記録。24時間の欠けチェックに使う。
CREATE TABLE IF NOT EXISTS collection_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue_id TEXT NOT NULL,
  slot TEXT NOT NULL,                 -- 予定時刻（UTC, 15分刻み）
  started_at TEXT NOT NULL,
  finished_at TEXT,
  block_number INTEGER,
  status TEXT NOT NULL,               -- running / ok / partial / failed / skipped
  pools_ok INTEGER NOT NULL DEFAULT 0,
  pools_failed INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_venue_slot ON collection_runs(venue_id, slot);

-- RPCの生の応答。計算式を変えたときに過去データで再計算するため。
CREATE TABLE IF NOT EXISTS raw_rpc (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER REFERENCES collection_runs(id),
  ts TEXT NOT NULL,
  block_number INTEGER,
  pool_id TEXT,
  kind TEXT NOT NULL,                 -- 例: pool_state / gauge_rewards
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_pool_ts ON raw_rpc(pool_id, ts);

CREATE TABLE IF NOT EXISTS pool_snapshots (
  pool_id TEXT NOT NULL REFERENCES pools(id),
  ts TEXT NOT NULL,                   -- 取得時刻（UTC）
  block_number INTEGER NOT NULL,      -- 読み取ったブロック
  run_id INTEGER REFERENCES collection_runs(id),
  price REAL,
  tick INTEGER,
  sqrt_price_x96 TEXT,
  fee INTEGER,
  liquidity_total TEXT,               -- プール全体のレンジ内流動性（pool.liquidity()）
  liquidity_staked_inrange TEXT,      -- ゲージにステークされたレンジ内流動性（報酬の取り分計算に使う）
  reward_rate_raw TEXT,               -- ゲージの報酬レート（トークンの最小単位/秒）
  reward_token TEXT,
  epoch_end TEXT,
  tvl REAL,
  volume_24h REAL,
  R_usd_day REAL,
  source TEXT NOT NULL,               -- 例: "rpc:public"
  PRIMARY KEY (pool_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_snap_block ON pool_snapshots(block_number);

CREATE TABLE IF NOT EXISTS token_prices (
  token TEXT NOT NULL,
  ts TEXT NOT NULL,
  price_usd REAL,
  volume_24h REAL,
  source TEXT NOT NULL,
  block_number INTEGER,
  PRIMARY KEY (token, ts, source)
);

-- 以下は M2 以降で使う（SPEC 6章）
CREATE TABLE IF NOT EXISTS scores (
  pool_id TEXT NOT NULL, ts TEXT NOT NULL, best_r REAL, income REAL, gamma REAL,
  rebalance REAL, hedge REAL, haircut REAL, net_daily_pct REAL, signal TEXT,
  reason_ja TEXT, warnings_json TEXT, PRIMARY KEY (pool_id, ts)
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pool_id TEXT NOT NULL, is_paper INTEGER NOT NULL,
  opened_at TEXT NOT NULL, closed_at TEXT, capital REAL, r REAL, lower REAL, upper REAL,
  status TEXT NOT NULL, close_reason TEXT
);
CREATE TABLE IF NOT EXISTS position_pnl (
  position_id INTEGER NOT NULL REFERENCES positions(id), ts TEXT NOT NULL, income REAL,
  direction REAL, gamma REAL, hedge REAL, other REAL, net REAL, PRIMARY KEY (position_id, ts)
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, is_paper INTEGER NOT NULL,
  tx_hash TEXT, kind TEXT NOT NULL, token TEXT, amount REAL, price_usd REAL, price_jpy REAL,
  position_id INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  ts TEXT NOT NULL, kind TEXT NOT NULL, title TEXT, body_ja TEXT, positions_json TEXT
);
CREATE TABLE IF NOT EXISTS learning_notes (
  ts TEXT NOT NULL, pool_id TEXT, title TEXT, body_ja TEXT
);
