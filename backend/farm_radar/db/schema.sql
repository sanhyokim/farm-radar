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
  -- 2026-09-30 追加（M6。週ごとの量で配る会場 = Alandale）
  reward_epoch_total_raw TEXT,        -- 今のエポックのボーナスの合計（最小単位）
  reward_manual_raw TEXT,             -- そのうち運営が手で足した分（続く保証がないので判定に使わない）
  -- 2026-10-01 追加（オーナー決定 A）: プールが持っているコインの量（最小単位）。緊急離脱の「プールのお金」に使う
  balance0_raw TEXT,
  balance1_raw TEXT,
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
  ts TEXT NOT NULL, pool_id TEXT, title TEXT, body_ja TEXT,
  topic TEXT                          -- 学びの種類（同じ話題が続かないように使う。M4）
);
-- 円のレート（M5a。frankfurter.app = 欧州中央銀行の参照レート。SPEC 12.4章）
-- day の日に使うレート。レートがない日（土日・祝日）は直前の営業日の値で、その日付を rate_date に入れる
CREATE TABLE IF NOT EXISTS fx_rates (
  day TEXT PRIMARY KEY,               -- 使う日（UTC の日付）
  jpy_per_usd REAL NOT NULL,
  rate_date TEXT NOT NULL,            -- レートの日付（営業日）
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
-- perp の資金調達率（M5a。Lighter の1時間ごとの実績。ショートの支払い。プラス = 払う）
CREATE TABLE IF NOT EXISTS funding_rates (
  market_id INTEGER NOT NULL, ts INTEGER NOT NULL, short_rate REAL NOT NULL,
  PRIMARY KEY (market_id, ts)
);
-- 練習の状態（M5b の停止・再開で使う。1行だけ）
CREATE TABLE IF NOT EXISTS paper_state (
  id INTEGER PRIMARY KEY CHECK (id = 1), stopped INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
  reason TEXT                         -- 止めた理由（M5b: オーナーの停止ボタン / 緊急離脱）
);

-- 守りの決まりの状態（N4b）。key ごとに JSON 1つ。例: loss_active = 今越えている損の線（同じ知らせを続けて出さないため）
CREATE TABLE IF NOT EXISTS guard_state (
  key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT
);

-- 練習の建玉の見張りの記録（M5b。SPEC 12.2章・付録A 3章）。画面の「見張りの記録」とタイムライン（M5c）に出す
-- level: caution（注意）/ rebalance（置き直し）/ exit（離脱）/ emergency（緊急離脱）/ info（停止・再開・見送りなど）
CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  position_id INTEGER,                -- 全体に関わるもの（停止・再開・緊急離脱のきっかけ）は NULL のこともある
  level TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- 例: edge_near / reward_shortfall / out_of_range / reward_token_drop / signal_red
  message_ja TEXT NOT NULL,
  action TEXT,                        -- 実際にしたこと: none / rebalanced / closed / closed_all / skipped_gas / stopped / resumed
  data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_events_pos ON risk_events(position_id, ts);

-- 会場プログラムの見張り（2026-09-29 オーナー決定。execution/contract_watch.py）。相手と項目ごとに最新の値
-- value: 読めた値 / unreadable（その関数がない = 画面で「未確認」）
CREATE TABLE IF NOT EXISTS contract_watch (
  address TEXT NOT NULL, item TEXT NOT NULL, label TEXT, value TEXT NOT NULL,
  first_seen TEXT NOT NULL, changed_at TEXT, checked_at TEXT,
  PRIMARY KEY (address, item)
);

-- USDG の外部の価格（GeckoTerminal。2026-09-29 オーナー決定: 2回続けて $0.98 未満なら緊急離脱）
CREATE TABLE IF NOT EXISTS stable_prices (
  ts TEXT NOT NULL, token TEXT NOT NULL, price REAL NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (token, ts)
);
CREATE INDEX IF NOT EXISTS idx_reviews_ts ON reviews(ts);

-- 2週間の評価（M5d。SPEC 11章）。オーナーがボタンで始める。status: running / stopped / finished
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL,
  -- status: running / stopped（オーナーがやめた）/ interrupted（建玉が全部閉じた。2026-10-01）。終わりは ends_at で分かる
  note TEXT                           -- 中断したときの、最後に閉じた建玉と理由（JSON）
);

-- perp（Lighter）の市場ごとの取引手数料（%）。開く・置き直す・閉じる時のヘッジの手数料に使う（2026-09-29 オーナー指示）
CREATE TABLE IF NOT EXISTS perp_fees (
  market_id INTEGER PRIMARY KEY, taker_pct REAL NOT NULL, maker_pct REAL, ts TEXT NOT NULL
);
-- ヘッジ先ごとの手数料と資金調達（SPEC 5.2.1章。ヘッジ先を差し替えられるように hedge_id を持つ。
-- 前の perp_fees・funding_rates は Lighter だけの古い記録として読むだけ）
CREATE TABLE IF NOT EXISTS hedge_fees (
  hedge_id TEXT NOT NULL, market_id INTEGER NOT NULL, symbol TEXT, taker_pct REAL, maker_pct REAL, ts TEXT NOT NULL,
  PRIMARY KEY (hedge_id, market_id)
);
CREATE TABLE IF NOT EXISTS hedge_funding (
  hedge_id TEXT NOT NULL, market_id INTEGER NOT NULL, ts INTEGER NOT NULL, short_rate REAL NOT NULL,
  PRIMARY KEY (hedge_id, market_id, ts)
);
-- ヘッジ先の担保の読み取り（アドレスを設定したときだけ。読み取りのみ）
CREATE TABLE IF NOT EXISTS hedge_accounts (
  hedge_id TEXT PRIMARY KEY, ts TEXT NOT NULL, collateral_usd REAL, available_usd REAL, positions_json TEXT,
  error TEXT
);

-- 毎朝のレポート（M4。SPEC 8章）。1日1通。送れたら sent_at が入る
CREATE TABLE IF NOT EXISTS daily_reports (
  day TEXT PRIMARY KEY,               -- 日本時間の日付（2026-09-30）
  ts TEXT NOT NULL,                   -- 作った時刻
  body_ja TEXT NOT NULL,
  sent_at TEXT
);

-- 候補の会場の一覧（週1回。SPEC 5.2.2章。2026-09-29 オーナー依頼 E1）。読み取りだけで集めたもの
CREATE TABLE IF NOT EXISTS discovery_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  trigger TEXT NOT NULL,              -- weekly（毎週） / startup（止めていた後） / button（今すぐ更新）
  status TEXT NOT NULL,               -- running / ok / partial（一部の読み取りに失敗） / error
  error TEXT,
  n_venues INTEGER,
  n_new INTEGER
);
-- 候補の会場。key は llama:<slug>@<チェーン> か gt:<ネットワーク>/<DEXのid>。data_json はその回の中身
CREATE TABLE IF NOT EXISTS discovery_venues (
  key TEXT PRIMARY KEY,
  source TEXT NOT NULL,               -- defillama / geckoterminal
  name TEXT NOT NULL,
  chain TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  first_run INTEGER NOT NULL,
  run_id INTEGER NOT NULL,            -- 最後に見つかった回
  data_json TEXT NOT NULL
);
-- 新しくボーナスが出始めたプール（DefiLlama。その回の分だけ）
CREATE TABLE IF NOT EXISTS discovery_pools (
  run_id INTEGER NOT NULL, pool TEXT NOT NULL, data_json TEXT NOT NULL,
  PRIMARY KEY (run_id, pool)
);
-- オーナーの判断（画面のボタン）。一覧が週ごとに入れ替わっても残る
CREATE TABLE IF NOT EXISTS discovery_decisions (
  key TEXT PRIMARY KEY,
  status TEXT NOT NULL,               -- study（調べる） / hold（保留） / skip（見送り）
  decided_at TEXT NOT NULL
);

-- 回数制限（429）を受けた記録（2026-09-30 オーナー条件。M6。SPEC 5.3章）。状態ページに24時間の回数を出し、
-- 直近に 429 があれば観察だけの会場（Alandale）の読み取りを休む
CREATE TABLE IF NOT EXISTS rate_limits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  host TEXT NOT NULL,                 -- 例: api.geckoterminal.com / rpc.mainnet.chain.robinhood.com
  kind TEXT NOT NULL                  -- external（外部データ） / rpc
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_ts ON rate_limits(ts);

-- 画面の「設定」で変える値（N2b。2026-10-02）。今は狙い利回り（target_apr_pct）だけ。
-- 無ければ config.yaml の opportunities.target_apr_pct（年30%。13.1 の3 オーナー決定）を使う
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 段階1の合図（プールのお金が1時間で大きく減った）のあとの「出ていたら／残っていたら」（2026-10-04 オーナー決定 A の追加1）。
-- 合図のときに出ていたら手もとに残った額（建玉の値打ち − 閉じる費用の見込み）と、そのあと 1・6・24 時間の同じ額を記録する。
-- 途中で閉じたら、閉じたときの額をそのあとの欄にも入れる。あとで「自動で出るようにするか」を数字で決めるため
CREATE TABLE IF NOT EXISTS stage1_watch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  pool_id TEXT NOT NULL,
  pair TEXT,
  ts TEXT NOT NULL,                   -- 合図の時刻
  drop_pct REAL,                      -- プールのお金の1時間の減り（%）
  funds_1h_ago REAL,                  -- プールのお金（1時間前。単位は unit）
  unit TEXT,
  value_usd REAL,                     -- 合図のときの建玉の値打ち
  exit_value_usd REAL,                -- そこで出ていたら（値打ち − 閉じる費用の見込み）
  stay_1h_usd REAL,                   -- 残っていたら（1時間後に出たときの額。途中で閉じたら閉じたときの額）
  stay_6h_usd REAL,
  stay_24h_usd REAL,
  closed_at TEXT,                     -- 24時間の間に閉じたとき
  close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_stage1_watch_pos ON stage1_watch(position_id, ts);

-- 保険（Lighter）の預け金の減り方（2026-10-04 オーナーのお願い1）。置き直すと保険の損は Lighter に、プールの得はチェーンに
-- 分かれてたまるので、長くいると Lighter の預け金だけが減る。練習の建玉ごとに1時間に1回記録する（読み取りと計算だけ）
CREATE TABLE IF NOT EXISTS hedge_margin_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  margin_usd REAL,            -- 預けたお金（建てたとき）
  hedge_pnl_usd REAL,         -- 保険の損益（資金調達料を含む。置き直しで固めた分も）
  equity_usd REAL,            -- 担保の今の価値 = 預けたお金 + 保険の損益
  maintenance_usd REAL,       -- 維持に要る額
  buffer_frac REAL,           -- 余裕 ÷ はじめの余裕
  notional_usd REAL,          -- 売りの額（今の値段）
  rebalances INTEGER          -- それまでの置き直しの回数
);
CREATE INDEX IF NOT EXISTS idx_hedge_margin_log_pos ON hedge_margin_log(position_id, ts);
