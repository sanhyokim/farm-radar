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

-- N4a: 入れる先の契約が、会場の公式の工場で作られたものか（feeds/venues.py。1日1回。答えが出たら30日は読み直さない）
CREATE TABLE IF NOT EXISTS venue_checks (
  chain_id INTEGER NOT NULL,
  address TEXT NOT NULL,               -- 入れる先の契約の住所（小文字）
  factory TEXT NOT NULL,               -- 会場の公式の工場の住所（小文字。venues/known/<id>.yaml）
  venue_id TEXT NOT NULL,
  function TEXT NOT NULL,              -- 聞き方（例: isMetaMorpho(address)）
  checked_at TEXT NOT NULL,
  verified INTEGER,                    -- 1 = 工場が作った。0 = 作っていない（または工場が答えない）
  error TEXT,
  PRIMARY KEY (chain_id, address, factory)
);

-- N4b: 金庫の運用先の見張り（feeds/vaults.py。2026-10-03 オーナー）。今の状態と、変わったときの記録
CREATE TABLE IF NOT EXISTS vault_states (
  chain_id INTEGER NOT NULL,
  address TEXT NOT NULL,               -- 金庫の住所（小文字）
  venue_id TEXT,
  kind TEXT,                           -- 読み方（morpho_vault_v2）
  checked_at TEXT NOT NULL,
  state_json TEXT,                     -- 運用先の一覧・すぐ引き出せる運用先・運用者・持ち主
  digest TEXT,
  error TEXT,
  PRIMARY KEY (chain_id, address)
);
CREATE TABLE IF NOT EXISTS vault_changes (
  chain_id INTEGER NOT NULL,
  address TEXT NOT NULL,
  detected_at TEXT NOT NULL,           -- 変わったのに気づいた時刻（前の回との間のどこかで変わった）
  before_json TEXT,
  after_json TEXT,
  PRIMARY KEY (chain_id, address, detected_at)
);

-- ここから N5a（「試す」のための記録。docs/n5-plan-2026-10-03.md の 2章。feeds/trial.py・feeds/shadow.py）

-- Merkl の配った額（預け方ごと。約2時間ごと。前の回と変わった行だけ）。amount は累計（受け取り済みを含む）、
-- pending はまだ配る木（root）に入っていない分。reason は「UNISWAP_V3_<プール>_<預け方の番号>」の形（幅は入っていない）
CREATE TABLE IF NOT EXISTS merkl_reward_snaps (
  ts TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  recipient TEXT NOT NULL,             -- 小文字
  reason TEXT NOT NULL,
  amount_raw TEXT,                     -- 最小単位（桁あふれを避けて文字で）
  claimed_raw TEXT,
  pending_raw TEXT,
  token TEXT,
  PRIMARY KEY (campaign_id, recipient, reason, ts)
);
CREATE TABLE IF NOT EXISTS merkl_reward_latest (
  campaign_id TEXT NOT NULL,
  recipient TEXT NOT NULL,
  reason TEXT NOT NULL,
  amount_raw TEXT,
  pending_raw TEXT,
  ts TEXT NOT NULL,
  PRIMARY KEY (campaign_id, recipient, reason)
);
-- キャンペーン全体で配った合計（変わったときだけ）。配る予定の総額は merkl_campaigns.amount_raw
CREATE TABLE IF NOT EXISTS merkl_reward_totals (
  ts TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  dist_chain_id INTEGER,
  amount_raw TEXT,
  PRIMARY KEY (campaign_id, ts)
);

-- 全部の行を読めた回の「確定した額（amount）＋まだ確定していない額（pending）」のキャンペーン全体の合計（変わったときだけ）。
-- 2026-10-04: merkl_reward_totals（/rewards/total）は、預け方ごとの額と同じ時点の値にならないことがある
-- （07:57 は合計と一致、12:07 は預け方の未確定の額が先に増えていた）。区切りの「全体で配った額」はこちらで数える
CREATE TABLE IF NOT EXISTS merkl_reward_sums (
  ts TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  sum_raw TEXT NOT NULL,
  rows INTEGER,
  complete INTEGER NOT NULL,           -- 1 = 全部のページを読めた
  PRIMARY KEY (campaign_id, ts)
);

-- DefiLlama の利回りと預かり額の毎日の記録（https://yields.llama.fi/chart/<プール>。1日1回。400日分）
CREATE TABLE IF NOT EXISTS llama_yield_history (
  pool TEXT NOT NULL,
  day TEXT NOT NULL,                   -- UTC の日付（DefiLlama の timestamp の日）
  tvl_usd REAL,
  apy REAL,
  apy_base REAL,
  apy_reward REAL,
  PRIMARY KEY (pool, day)
);

-- Lighter の資金調達率の過去（/api/v1/fundings、1時間ごと。応答の値のまま。単位と向きは N5b で照合してから使う）
CREATE TABLE IF NOT EXISTS lighter_funding_history (
  market_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,                 -- UNIX 秒
  symbol TEXT,
  rate REAL,
  value REAL,
  direction TEXT,
  PRIMARY KEY (market_id, ts)
);

-- Lighter の値段の過去（1時間ごとのろうそく足。/api/v1/candles。1日1回、最初は90日分）。
-- 保険の預け金の「50% に耐える」を、長い期間の大きな値上がりで見直すため（2026-10-04 オーナーの質問4）
CREATE TABLE IF NOT EXISTS lighter_price_history (
  market_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,                 -- 足の始まり（UNIX 秒）
  symbol TEXT,
  open REAL, high REAL, low REAL, close REAL,
  PRIMARY KEY (market_id, ts)
);

-- Lighter の Robinhood Chain 版（別の取引所。預け金は USDG。市場の番号は本体と別。2026-10-04 オーナー決定 ②A）。
-- 本体と同じ形で、別の表に保存する（番号がぶつからないように）。読み取りだけ
CREATE TABLE IF NOT EXISTS lighter_rh_markets (
  market_id INTEGER PRIMARY KEY,
  symbol TEXT NOT NULL,
  status TEXT,
  taker_pct REAL,
  maker_pct REAL,
  initial_margin_fraction INTEGER,     -- default_initial_margin_fraction（本体と同じ欄。÷10000 が割合）
  min_initial_margin_fraction INTEGER, -- いちばん高い倍率のときの最初に要る割合（参考）
  maintenance_margin_fraction INTEGER,
  open_interest REAL,
  daily_quote_volume REAL,
  mark_price REAL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lighter_rh_funding_snaps (
  ts TEXT NOT NULL,
  market_id INTEGER NOT NULL,
  symbol TEXT,
  rate_8h REAL,
  PRIMARY KEY (ts, market_id)
);
CREATE TABLE IF NOT EXISTS lighter_rh_funding_history (
  market_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  symbol TEXT,
  rate REAL,
  value REAL,
  direction TEXT,
  PRIMARY KEY (market_id, ts)
);
CREATE TABLE IF NOT EXISTS lighter_rh_price_history (
  market_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  symbol TEXT,
  open REAL, high REAL, low REAL, close REAL,
  PRIMARY KEY (market_id, ts)
);

-- Aero の公式の住所のファイル（公開のコード置き場の deployment-addresses。1時間に1回）
CREATE TABLE IF NOT EXISTS aero_address_files (
  name TEXT PRIMARY KEY,
  sha TEXT,
  via TEXT,                            -- "list"（一覧で読んだ）/ "guess"（名前を1つずつ確かめた）
  size INTEGER,
  url TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  changed_at TEXT                      -- 中身が変わったのに気づいた時刻
);

-- 影の記録（毎時間の見込みのメモ。判断③A: 一覧に出る行のうち、狙い利回り以上と上位30行。建玉は作らない）
CREATE TABLE IF NOT EXISTS shadow_predictions (
  ts TEXT NOT NULL,
  opp_key TEXT NOT NULL,
  amount REAL NOT NULL,
  chain TEXT,
  kind TEXT,
  rank INTEGER,                        -- $1,000 の並び順（0 から）
  target_apr_pct REAL,
  apr_cautious REAL,                   -- 控えめの見込みの年利（%）
  apr_normal REAL,
  net_cautious REAL,                   -- 1日に残る額（入る・出る費用を引いたあと。ドル）
  net_normal REAL,
  hedge INTEGER,                       -- 控えめの見込みでよい方が保険あり
  recommended INTEGER,
  detail_json TEXT,                    -- 内訳（ボーナスの取り分・値動きの損・費用・保険・幅など）
  PRIMARY KEY (ts, opp_key, amount)
);
CREATE INDEX IF NOT EXISTS idx_shadow_key ON shadow_predictions(opp_key, ts);

-- N5c: Merkl の答え合わせ。預け方1つごとの幅と量（Uniswap v4 の公式の PositionManager。feeds/positions.py）。
-- 幅（tick_lower・tick_upper）は変わらないので最初に読めた値、量（liquidity）は毎回の値
CREATE TABLE IF NOT EXISTS merkl_position_snaps (
  chain_id INTEGER NOT NULL,
  token_id INTEGER NOT NULL,           -- 預け方の番号（Merkl の reason の末尾）
  ts TEXT NOT NULL,
  pool_id TEXT,                        -- キャンペーンのプール（小文字）
  tick_lower INTEGER,
  tick_upper INTEGER,
  liquidity TEXT,                      -- 大きな整数なので文字で
  error TEXT,
  PRIMARY KEY (chain_id, token_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_merkl_reward_reason ON merkl_reward_snaps(campaign_id, reason, ts);

-- Merkl の分母 B（幅の中だけ）の検証（2026-10-04 指示書）。キャンペーンのプールの、預け方を足した・減らした記録
-- （Uniswap v4 の公式の PoolManager の ModifyLiquidity。チェーンの記録。feeds/pool_history.py）。
-- 読んだ範囲（next_block の手前まで）を覚えて、同じ記録を読み直さない。途中で止まっても、続きから読む。
CREATE TABLE IF NOT EXISTS pool_liq_progress (
  chain_id INTEGER NOT NULL,
  pool_id TEXT NOT NULL,               -- 小文字
  manager TEXT,                        -- 読んだ PoolManager（公式の住所と同じものだけ）
  keep_from_ts INTEGER NOT NULL,       -- これより前の記録は、預け方ごとの合計（pool_liq_base）にまとめる
  keep_from_block INTEGER,             -- keep_from_ts のときのブロック（ブロックの時刻を二分探索で確かめた）
  next_block INTEGER NOT NULL DEFAULT 0,
  done_ts INTEGER,                     -- next_block の手前のブロックの時刻（ここまでの預け方は確かめられる）
  events INTEGER NOT NULL DEFAULT 0,   -- これまでに読んだ記録の数
  calls INTEGER NOT NULL DEFAULT 0,    -- これまでの読み取りの回数
  error TEXT,
  updated_at TEXT,
  PRIMARY KEY (chain_id, pool_id)
);
-- keep_from_ts より前の記録をまとめた、預け方ごとの量（0 になったものは消す）
CREATE TABLE IF NOT EXISTS pool_liq_base (
  chain_id INTEGER NOT NULL,
  pool_id TEXT NOT NULL,
  pos_key TEXT NOT NULL,               -- 預け方の印（v4 の Position の鍵: keccak(owner, 下, 上, salt)）
  owner TEXT NOT NULL,                 -- 預けた契約（公式の PositionManager なら、salt が預け方の番号）
  tick_lower INTEGER NOT NULL,
  tick_upper INTEGER NOT NULL,
  salt TEXT NOT NULL,
  liquidity TEXT NOT NULL,             -- 大きな整数なので文字で
  PRIMARY KEY (chain_id, pool_id, pos_key)
);
-- keep_from_ts 以後の記録（1件ずつ）
CREATE TABLE IF NOT EXISTS pool_liq_events (
  chain_id INTEGER NOT NULL,
  pool_id TEXT NOT NULL,
  block INTEGER NOT NULL,
  log_index INTEGER NOT NULL,
  ts INTEGER NOT NULL,                 -- ブロックの時刻（UNIX 秒。前後の目印のブロックの時刻から比例で出す。ずれは約20秒まで）
  pos_key TEXT NOT NULL,
  owner TEXT NOT NULL,
  tick_lower INTEGER NOT NULL,
  tick_upper INTEGER NOT NULL,
  salt TEXT NOT NULL,
  delta TEXT NOT NULL,                 -- 足した量（減らしたときは負）
  PRIMARY KEY (chain_id, pool_id, block, log_index)
);
CREATE INDEX IF NOT EXISTS idx_pool_liq_events_ts ON pool_liq_events(chain_id, pool_id, ts);
-- ブロックの時刻の目印（チェーンの記録）。Robinhood Chain の公開の読み取り口は、記録の blockTimestamp を 0 で返す
-- （2026-10-04 に確かめた）ので、目印のブロックの時刻を読んで、その間は比例で出す
CREATE TABLE IF NOT EXISTS chain_block_times (
  chain_id INTEGER NOT NULL,
  block INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  PRIMARY KEY (chain_id, block)
);
-- 預け方の歴史の読み取りの1回ごとの記録（パソコンの1行の更新のまとめで「今回」の進み具合を出すため。2026-10-04 指示書）
CREATE TABLE IF NOT EXISTS pool_liq_runs (
  ts TEXT PRIMARY KEY,
  targets INTEGER NOT NULL,            -- 読む対象のプールの数
  calls INTEGER NOT NULL,              -- この回の読み取りの回数
  events INTEGER NOT NULL,             -- この回に読めた記録の数
  pools_read INTEGER NOT NULL,         -- この回に今のブロックまで読み終えたプールの数
  errors INTEGER NOT NULL,             -- この回に読めなかったプールの数
  stopped TEXT                         -- 回数の上限・回数制限で、この回を途中でやめた理由（続きは次の回）
);
