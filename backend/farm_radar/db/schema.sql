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
  discovered_at TEXT NOT NULL,
  token0_symbol TEXT,
  token1_symbol TEXT,
  token0_decimals INTEGER,
  token1_decimals INTEGER
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

-- 収集が止まっていた期間（パソコンのスリープ・再起動・Docker停止など）。
-- 画面に「欠損」として表示し、M5のペーパートレード評価ではこの期間の損益を推定扱いにして分ける。
CREATE TABLE IF NOT EXISTS collection_gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue_id TEXT NOT NULL,
  start_slot TEXT NOT NULL,           -- 欠けた最初の予定時刻（UTC）
  end_slot TEXT NOT NULL,             -- 欠けた最後の予定時刻（UTC）
  missed_slots INTEGER NOT NULL,
  detected_at TEXT NOT NULL,
  UNIQUE (venue_id, start_slot)
);

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
  -- 2026-09-27 追加（オーナー指示: 報酬の毎秒量とエポックをスナップショットごとに保存）
  block_time TEXT,                    -- 読み取ったブロックの時刻（UTC）
  epoch_start TEXT,
  period_finish TEXT,                 -- ゲージの今の配布期間の終わり
  reward_rate_effective_raw TEXT,     -- 今実際に出ている報酬レート（配布期間外・ゲージ停止中は0）
  gauge_alive INTEGER,
  unstaked_fee INTEGER,               -- ステークしないLPから取る手数料の割合（1e-6単位）
  epoch_just_flipped INTEGER,         -- 1 = エポック更新直後（報酬の値がまだ落ち着いていない可能性）
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

-- 通知すべき出来事（M4 で Discord / Telegram に送る。それまではAPIとログで確認する）
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  pool_id TEXT,
  kind TEXT NOT NULL,                 -- 例: reward_rate_drop
  level TEXT NOT NULL,                -- info / warning / major
  message_ja TEXT NOT NULL,
  data_json TEXT,
  dedupe_key TEXT UNIQUE,             -- 同じ出来事を二重に記録しないための鍵
  notified_at TEXT                    -- 通知を送った時刻（M4）
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

-- 以下は M2 以降で使う（SPEC 6章）
-- スコアと判定（M2。1時間ごと）。金額はドル/日、net_daily_pct は総資産あたりの%（判定に使う）。
CREATE TABLE IF NOT EXISTS scores (
  pool_id TEXT NOT NULL, ts TEXT NOT NULL, best_r REAL, income REAL, gamma REAL,
  rebalance REAL, hedge REAL, haircut REAL, net_daily_pct REAL, signal TEXT,
  reason_ja TEXT, warnings_json TEXT,
  venue_id TEXT,
  block_number INTEGER,               -- 使ったスナップショットのブロック
  direction_risk REAL,                -- ヘッジできないトークンの値下がりの損の見込み
  net_daily_pct_lp REAL,              -- 建玉あたりの%（表示用。判定には使わない）
  mode TEXT,                          -- staked（ボーナス）/ unstaked（手数料）
  in_range_ratio REAL,                -- レンジ内の時間の割合（置き直す前提。判定に使う）
  in_range_ratio_hold REAL,           -- 参考値（置きっぱなしの場合）
  sigma_pair REAL, sigma_token0 REAL, sigma_token1 REAL,
  vol_source TEXT,                    -- own（自分の記録）/ external（GeckoTerminal で補った）
  has_perp INTEGER,                   -- 値動きのあるトークンがすべてヘッジできる
  epoch_just_flipped INTEGER,
  tvl_usd REAL,
  details_json TEXT,                  -- レンジ幅ごとの計算結果と、使った入力
  PRIMARY KEY (pool_id, ts)
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pool_id TEXT NOT NULL, is_paper INTEGER NOT NULL,
  opened_at TEXT NOT NULL, closed_at TEXT, capital REAL, r REAL, lower REAL, upper REAL,
  status TEXT NOT NULL, close_reason TEXT
);
CREATE TABLE IF NOT EXISTS position_pnl (
  position_id INTEGER NOT NULL REFERENCES positions(id), ts TEXT NOT NULL, income REAL,
  direction REAL, gamma REAL, hedge REAL, other REAL, net REAL,
  is_estimated INTEGER NOT NULL DEFAULT 0,   -- 欠損期間中の値なら1（評価の計算から分ける）
  PRIMARY KEY (position_id, ts)
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
