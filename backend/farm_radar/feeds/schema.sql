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

-- ここから N2b（機会の一覧の計算に使う。2026-10-02）。古い表に足す列は store.py の _ADD_COLUMNS

-- Lighter（保険の売り場）の銘柄（1日1回。/api/v1/orderBookDetails）
CREATE TABLE IF NOT EXISTS lighter_markets (
  market_id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL,
  status TEXT,
  taker_pct REAL,
  maker_pct REAL,
  initial_margin_fraction INTEGER,     -- 応答の値のまま（単位は公式の資料で確かめてから使う）
  maintenance_margin_fraction INTEGER,
  open_interest REAL,
  daily_quote_volume REAL,
  mark_price REAL,
  updated_at TEXT NOT NULL
);

-- Lighter の資金調達率（1時間に1回。/api/v1/funding-rates の exchange=lighter。8時間あたりの割合）
CREATE TABLE IF NOT EXISTS lighter_funding_snaps (
  ts TEXT NOT NULL,
  market_id INTEGER NOT NULL,
  symbol TEXT,
  rate_8h REAL,
  PRIMARY KEY (ts, market_id)
);

-- コインの1時間ごとの値段（1日1回。DefiLlama の coins の chart。登録したチェーンの機会に出てくるコインだけ）
CREATE TABLE IF NOT EXISTS token_prices (
  coin TEXT NOT NULL,                  -- "<DefiLlama のチェーン名>:<住所>"
  ts INTEGER NOT NULL,                 -- UNIX 秒
  price REAL NOT NULL,
  PRIMARY KEY (coin, ts)
);
CREATE TABLE IF NOT EXISTS token_meta (
  coin TEXT PRIMARY KEY,
  symbol TEXT,
  chain_id INTEGER,
  address TEXT,
  confidence REAL,
  updated_at TEXT NOT NULL
);

-- 預かり証の中身（N2c。SPEC 13.1 の5）: 値段の記録がないボーナスのコインが、決まった形の金庫（ERC-4626）の預かり証か。
-- チェーンの公開の読み取り口で asset()・convertToAssets() を読む（読み取りだけ）。1日1回
CREATE TABLE IF NOT EXISTS receipt_checks (
  chain_id INTEGER NOT NULL,
  address TEXT NOT NULL,               -- 小文字
  checked_at TEXT NOT NULL,
  is_vault INTEGER NOT NULL,           -- 1 = asset() と convertToAssets() に答えた
  name TEXT,
  symbol TEXT,
  asset TEXT,                          -- 中身のコインの住所（小文字）
  asset_symbol TEXT,
  assets_per_share REAL,               -- 預かり証1枚あたりの中身の量（中身のコインの単位）
  verified INTEGER,                    -- 契約の中身が公開・確認済み（Blockscout か Sourcify）。分からなければ NULL
  verified_by TEXT,                    -- "blockscout" / "sourcify"
  error TEXT,
  PRIMARY KEY (chain_id, address)
);

-- N3: 幅に配るプール（Uniswap v3 / v4 の形）の今の状態（チェーンの公開の読み取り口で読む。1時間に1回）
CREATE TABLE IF NOT EXISTS pool_state_snaps (
  chain_id INTEGER NOT NULL,
  pool_id TEXT NOT NULL,               -- v3 はプールの住所、v4 は poolId（どちらも小文字）
  checked_at TEXT NOT NULL,
  kind TEXT NOT NULL,                  -- "v3" / "v4"
  block INTEGER,
  sqrt_price_x96 TEXT,                 -- 大きな整数なので文字で
  tick INTEGER,
  liquidity TEXT,                      -- 今の値段のところの流動性（幅の中にいる人の合計）
  lp_fee INTEGER,                      -- 手数料の段（100万分の1。500 = 0.05%）
  price REAL,                          -- token0 1個あたりの token1（桁を調整した値）
  official INTEGER,                    -- 1 = 公式の住所（chains/*.yaml の uniswap）と確かめた
  error TEXT,
  hooks TEXT,                          -- v4 のフックの住所（0x000… = フックなし。PositionManager.poolKeys。分からなければ NULL）
  tick_spacing INTEGER,
  PRIMARY KEY (chain_id, pool_id, checked_at)
);
CREATE INDEX IF NOT EXISTS pool_state_snaps_latest ON pool_state_snaps(chain_id, pool_id, checked_at);
