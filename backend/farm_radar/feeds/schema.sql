-- 一覧の保存（N2a。作り直し（渡り鳥）SPEC 13.4）のデータベース（data/feeds.sqlite3）。
-- 量が多いので、今の版のデータベース（farm_radar.sqlite3）とは分ける。元の応答は data/feeds/ に圧縮して残す。

CREATE TABLE IF NOT EXISTS feeds_schema_version (version INTEGER NOT NULL);

-- 1回の読み取りの記録。status: running / ok / error / rate_limited（429。画面では「不明」）
CREATE TABLE IF NOT EXISTS feed_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  started_at TEXT NOT NULL,            -- UTC
  finished_at TEXT,
  status TEXT NOT NULL,
  items INTEGER,                       -- 読めた件数
  new_items INTEGER,                   -- 前の回までになかった件数（最初の回は数えない）
  gone_items INTEGER,                  -- 前の回にあって、この回になかった件数
  pages INTEGER,
  bytes INTEGER,                       -- 元の応答の大きさ（圧縮する前）
  raw_path TEXT,                       -- 元の応答を残したファイル（data/feeds/ からの場所）
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_runs_source ON feed_runs(source, started_at);

-- 一覧に出てきたもの（機会・会場・チェーン・お知らせ）。いつ初めて見て、いつ最後に見たか。
CREATE TABLE IF NOT EXISTS feed_items (
  source TEXT NOT NULL,
  key TEXT NOT NULL,
  first_seen TEXT NOT NULL,            -- UTC（初めて見た回の開始時刻）
  last_seen TEXT NOT NULL,             -- UTC（最後に見た回の開始時刻）
  baseline INTEGER NOT NULL DEFAULT 0, -- 1 = 保存を始めた最初の回からあった（「新しい」に数えない）
  name TEXT,
  chain TEXT,
  info_json TEXT,                      -- 一覧に出す短い情報（最新の値）
  PRIMARY KEY (source, key)
);
CREATE INDEX IF NOT EXISTS idx_feed_items_new ON feed_items(source, first_seen);

-- Merkl の機会の数字（15分ごと）。名前などの変わらない情報は feed_items にある。
-- apr は Merkl の表示のまま（例: 5 = 年5% と見られる。N2b で確かめてから計算に使う）
CREATE TABLE IF NOT EXISTS merkl_opportunity_snaps (
  ts TEXT NOT NULL,
  opportunity_id TEXT NOT NULL,
  status TEXT,
  apr REAL,
  max_apr REAL,
  native_apr REAL,
  tvl REAL,
  daily_rewards REAL,
  live_campaigns INTEGER,
  PRIMARY KEY (ts, opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_merkl_snaps_opp ON merkl_opportunity_snaps(opportunity_id, ts);

-- Merkl のキャンペーン（ボーナスを配る1つ1つの約束）。最新の値で上書きし、初めて見た時刻を残す。
CREATE TABLE IF NOT EXISTS merkl_campaigns (
  campaign_id TEXT PRIMARY KEY,        -- Merkl の campaignId（無ければ id）
  merkl_id TEXT,
  opportunity_id TEXT,
  chain_id INTEGER,                    -- 預ける側のチェーン（computeChainId）
  distribution_chain_id INTEGER,       -- 受け取る側のチェーン
  type TEXT,
  sub_type TEXT,
  distribution_type TEXT,
  start_ts INTEGER,                    -- UNIX 秒
  end_ts INTEGER,
  amount_raw TEXT,                     -- 配る量（最小単位。桁あふれを避けて文字で）
  reward_symbol TEXT,
  reward_address TEXT,
  reward_decimals INTEGER,
  reward_price REAL,
  daily_rewards REAL,
  apr REAL,
  creator TEXT,
  created_at INTEGER,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merkl_campaigns_opp ON merkl_campaigns(opportunity_id);

-- DefiLlama の利回りのうち、ボーナスのあるもの（1日1回）
CREATE TABLE IF NOT EXISTS yield_snaps (
  day TEXT NOT NULL,                   -- 日本時間の日付
  pool TEXT NOT NULL,
  chain TEXT,
  project TEXT,
  symbol TEXT,
  tvl_usd REAL,
  apy_base REAL,
  apy_reward REAL,
  apy REAL,
  PRIMARY KEY (day, pool)
);

-- 429（回数制限）の記録（ratelimit.record が書く）
CREATE TABLE IF NOT EXISTS rate_limits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  host TEXT NOT NULL,
  kind TEXT NOT NULL
);
