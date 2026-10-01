"""config.yaml と venues/*.yaml、.env の読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 12.3章: 今の段階で実装するモードはこの2つだけ。
ALLOWED_MODES = ("observe", "paper")

# config.yaml と venues/ がある場所。Docker では環境変数 FARM_RADAR_ROOT で指定する。
REPO_ROOT = Path(os.environ.get("FARM_RADAR_ROOT") or Path(__file__).resolve().parents[2])


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RpcSettings:
    max_retries: int = 3
    backoff_seconds: float = 1.0
    timeout_seconds: float = 15.0
    extra_urls: tuple[str, ...] = ()
    latest_cache_seconds: float = 5.0


@dataclass(frozen=True)
class ScoringSettings:
    """スコア計算と判定の設定（SPEC 3.2章・4章）。config.yaml の scoring / signal から読む。"""
    every_minutes: int = 60
    total_capital_usd: float = 1000.0
    allocation_lp: float = 0.55
    allocation_hedge_margin: float = 0.40
    allocation_reserve: float = 0.05
    ranges_pct: tuple[float, ...] = (0.5, 1, 2, 3, 5, 10, 15)
    rebalance_wait_minutes: float = 15.0
    gas_units_per_tx: int = 600_000
    swap_ratio: float = 0.5
    slippage_trade_usd: float | None = None      # 両替のずれを見積もる金額（None = LPに置く額）
    slippage_fallback_stable_stock_pct: float = 0.1
    slippage_fallback_other_pct: float = 1.0
    volume_suspicious_tvl_multiple: float = 10.0   # 1日の取引量がTVLのこの倍を超えたら警告し、手数料の収入は0として計算
    hedge_taker_fee_pct: float = 0.0
    count_funding_income: bool = False
    reward_sell_hours: float = 1.0                 # 参考値「報酬をすぐ売る前提」: 受け取ってから売るまでの時間
    volatility_days: float = 7.0
    external_refresh_hours: float = 3.0
    green_min_pct: float = 0.30
    yellow_min_pct: float = 0.10
    green_min_tvl_usd: float = 200_000.0
    reward_token_7d_major_pct: float = -30.0
    too_high_pct: float = 5.0



def _scoring(raw: dict[str, Any]) -> ScoringSettings:
    sc = raw.get("scoring") or {}
    sig = raw.get("signal") or {}
    alloc = sc.get("allocation") or {}
    hedge = sc.get("hedge") or {}
    slip = sc.get("slippage") or {}
    d = ScoringSettings()
    out = ScoringSettings(
        every_minutes=int(sc.get("every_minutes", d.every_minutes)),
        total_capital_usd=float(sc.get("total_capital_usd", d.total_capital_usd)),
        allocation_lp=float(alloc.get("lp", d.allocation_lp)),
        allocation_hedge_margin=float(alloc.get("hedge_margin", d.allocation_hedge_margin)),
        allocation_reserve=float(alloc.get("reserve", d.allocation_reserve)),
        ranges_pct=tuple(float(x) for x in sc.get("ranges_pct", d.ranges_pct)),
        rebalance_wait_minutes=float(sc.get("rebalance_wait_minutes", d.rebalance_wait_minutes)),
        gas_units_per_tx=int(sc.get("gas_units_per_tx", d.gas_units_per_tx)),
        swap_ratio=float(sc.get("swap_ratio", d.swap_ratio)),
        slippage_trade_usd=(float(slip["trade_usd"]) if slip.get("trade_usd") is not None else None),
        slippage_fallback_stable_stock_pct=float(
            slip.get("fallback_stable_stock_pct", d.slippage_fallback_stable_stock_pct)),
        slippage_fallback_other_pct=float(slip.get("fallback_other_pct", d.slippage_fallback_other_pct)),
        volume_suspicious_tvl_multiple=float(sc.get("volume_suspicious_tvl_multiple",
                                                    sc.get("volume_cap_tvl_multiple", d.volume_suspicious_tvl_multiple))),
        hedge_taker_fee_pct=float(hedge.get("taker_fee_pct", d.hedge_taker_fee_pct)),
        count_funding_income=bool(hedge.get("count_funding_income", d.count_funding_income)),
        reward_sell_hours=float(sc.get("reward_sell_hours", d.reward_sell_hours)),
        volatility_days=float(sc.get("volatility_days", d.volatility_days)),
        external_refresh_hours=float(sc.get("external_refresh_hours", d.external_refresh_hours)),
        green_min_pct=float(sig.get("green_min_pct", d.green_min_pct)),
        yellow_min_pct=float(sig.get("yellow_min_pct", d.yellow_min_pct)),
        green_min_tvl_usd=float(sig.get("green_min_tvl_usd", d.green_min_tvl_usd)),
        reward_token_7d_major_pct=float(sig.get("reward_token_7d_major_pct", d.reward_token_7d_major_pct)),
        too_high_pct=float(sig.get("too_high_pct", d.too_high_pct)),
    )
    total = out.allocation_lp + out.allocation_hedge_margin + out.allocation_reserve
    if abs(total - 1) > 1e-6:
        raise ConfigError(f"scoring.allocation の合計が1になっていません（今は {total}）。")
    if out.every_minutes <= 0 or not out.ranges_pct or min(out.ranges_pct) <= 0 or max(out.ranges_pct) >= 100:
        raise ConfigError("scoring の every_minutes / ranges_pct を確認してください。")
    return out


@dataclass(frozen=True)
class NotifySettings:
    """通知の設定（M4。SPEC 8章）。config.yaml の notify から読む。トークンなどは .env にだけ書く。"""
    daily_report_hour_jst: int = 8        # 毎朝のレポートを送る時刻（日本時間）
    daily_report_minute: int = 0
    daily_report_top: int = 5             # レポートに載せる上位の件数
    error_repeat_minutes: int = 60        # 同じエラーの通知はこの分数に1回だけ
    max_age_hours: float = 24.0           # これより古いお知らせは送らない（パソコンを長く止めていたとき用）
    send_every_minutes: int = 1           # 送っていないお知らせを確かめる間隔


def _notify(raw: dict[str, Any]) -> NotifySettings:
    n = raw.get("notify") or {}
    d = NotifySettings()
    hh, mm = str(n.get("daily_report_time_jst", "08:00")).split(":")
    out = NotifySettings(
        daily_report_hour_jst=int(hh), daily_report_minute=int(mm),
        daily_report_top=int(n.get("daily_report_top", d.daily_report_top)),
        error_repeat_minutes=int(n.get("error_repeat_minutes", d.error_repeat_minutes)),
        max_age_hours=float(n.get("max_age_hours", d.max_age_hours)),
        send_every_minutes=int(n.get("send_every_minutes", d.send_every_minutes)),
    )
    if not (0 <= out.daily_report_hour_jst < 24 and 0 <= out.daily_report_minute < 60):
        raise ConfigError("notify.daily_report_time_jst は \"08:00\" のように書いてください。")
    if out.error_repeat_minutes <= 0 or out.send_every_minutes <= 0:
        raise ConfigError("notify の分数は1以上にしてください。")
    return out


@dataclass(frozen=True)
class RiskSettings:
    """練習の建玉の見張り（M5b。SPEC 12.2章・付録A 3章の初期値）。config.yaml の risk から読む。"""
    # 米国市場の開場前後（ニューヨーク時間。夏時間・冬時間に自動で合わせる）。この間は見張りの間隔を短くする
    # 2026-09-29 オーナー決定: 開く30分前〜開いた1時間後
    fast_window_ny: tuple[str, str] = ("09:00", "10:30")
    fast_minutes: int = 5
    caution_edge_pct: float = 1.0               # 注意: レンジの端までこの%未満
    caution_reward_shortfall_pct: float = 40.0  # 注意: 報酬の実績が予測よりこの%以上少ない
    caution_min_hours: float = 2.0              # 報酬の実績と予測を比べるのは、この時間分の記録がたまってから
    rebalance_after_minutes: float = 15.0       # 置き直し: レンジの外にこの分数いたら
    rebalance_min_net_pct: float = 0.0          # 置き直し先の純日利（総資産あたり%）がこれ以下なら、置き直さずに離脱
    exit_reward_token_24h_pct: float = -20.0    # 離脱: 報酬トークンが24時間でこの%以下
    # 緊急離脱: プールのお金（プールが持っているコインの量 × 今の値段）が1時間でこの%以上減った（2026-10-01 オーナー決定 A。
    # 前の名前 emergency_liquidity_drop_1h_pct も読む。前はレンジ内の流動性で比べていて、値段が動くだけで働いた）
    emergency_pool_funds_drop_1h_pct: float = 50.0
    # 注意（記録と表示だけ）: 今の値段のところの流動性（レンジ内の流動性）が1時間でこの%以上減った。
    # ボーナスの取り分の計算に関わるため（2026-10-01 オーナー決定。緊急離脱には使わない）
    caution_active_liquidity_drop_1h_pct: float = 50.0
    emergency_daily_loss_pct: float = 5.0       # 緊急離脱: 今日（日本時間）の損が総資産（建玉の投入額の合計）のこの%に達した
    # 2026-09-29 オーナー決定（SPEC 12.2章）
    exit_dump_1h_pct: float = -15.0             # 離脱（投げ売り）: 値動きする側のトークンがプール価格で1時間でこの%以下
    exit_dump_24h_pct: float = -30.0            #   または24時間でこの%以下
    emergency_usdg_below: float = 0.98          # 緊急離脱: USDG の外部の価格がこの値未満
    emergency_usdg_times: int = 2               #   が、この回数続いた
    caution_hedge_cost_pct: float = 50.0        # 注意: ヘッジの1日の費用が、報酬（1日あたり）のこの%を超えた
    contract_watch: bool = True                 # 会場プログラムの停止・持ち主・入れ替えを読み取りで見張る（変わったら緊急離脱）
    max_swap_slippage_pct: float = 1.0          # 離脱の両替: ずれがこの%を超えるなら分けて売る
    max_gas_usd_per_tx: float = 1.0             # ふつうの離脱と置き直しは、ガス代がこれを超えたら見送る（緊急離脱は実行する）
    # 実績の日利の見せ方（2026-09-29 オーナー指示）
    actual_min_hours: float = 6.0               # 始めてからこの時間未満は「参考（データ不足）」として小さく出す
    compare_min_hours: float = 24.0             # この時間以上たってから「予測との比較」を有効にする
    # ボーナスが減ったときの比べ方（2026-09-30 オーナー決定③。SPEC 8.5章）
    bonus_drop_ratio: float = 0.5               # 切り替えのあとのボーナスが前の週のこの割合以下になったら比べる
    bonus_drop_wait_hours: float = 6.0          # ボーナスが0のままなら、切り替えからこの時間待ってから比べる（配られる前の0と区別）
    bonus_drop_action: str = "record"           # record = 記録と通知だけ（評価の間はこれ。動かす部分はまだない）


def _risk(raw: dict[str, Any]) -> RiskSettings:
    r = dict(raw.get("risk") or {})
    d = RiskSettings()
    if "emergency_pool_funds_drop_1h_pct" not in r and "emergency_liquidity_drop_1h_pct" in r:
        r["emergency_pool_funds_drop_1h_pct"] = r["emergency_liquidity_drop_1h_pct"]   # 前の名前（2026-10-01 まで）
    win = str(r.get("fast_window_ny", "-".join(d.fast_window_ny))).split("-")
    if len(win) != 2:
        raise ConfigError("risk.fast_window_ny は \"09:00-10:30\" のように書いてください。")
    out = RiskSettings(
        fast_window_ny=(win[0].strip(), win[1].strip()),
        **{k: type(getattr(d, k))(r.get(k, getattr(d, k))) for k in (
            "fast_minutes", "caution_edge_pct", "caution_reward_shortfall_pct", "caution_min_hours",
            "rebalance_after_minutes", "rebalance_min_net_pct", "exit_reward_token_24h_pct",
            "emergency_pool_funds_drop_1h_pct", "caution_active_liquidity_drop_1h_pct",
            "emergency_daily_loss_pct", "max_swap_slippage_pct",
            "max_gas_usd_per_tx", "actual_min_hours", "compare_min_hours", "exit_dump_1h_pct", "exit_dump_24h_pct",
            "emergency_usdg_below", "emergency_usdg_times", "caution_hedge_cost_pct", "contract_watch",
            "bonus_drop_ratio", "bonus_drop_wait_hours", "bonus_drop_action")},
    )
    for hm in out.fast_window_ny:
        hh, _, mm = hm.partition(":")
        if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60):
            raise ConfigError("risk.fast_window_ny は \"09:00-10:30\" のように書いてください。")
    if out.bonus_drop_action != "record":
        raise ConfigError("risk.bonus_drop_action は今は record（記録と通知だけ）しか使えません。")
    if not 0 < out.bonus_drop_ratio < 1:
        raise ConfigError("risk.bonus_drop_ratio は 0 より大きく 1 より小さい数（0.5 など）にしてください。")
    if out.fast_minutes <= 0 or 60 % out.fast_minutes:
        raise ConfigError("risk.fast_minutes は60を割り切れる数（5 など）にしてください。")
    return out


@dataclass(frozen=True)
class ReviewSettings:
    """定時レビューと資産の見通し（M5c。SPEC 8.5章・7.4章）。config.yaml の review から読む。"""
    every_minutes: int = 30                 # 定時レビューを作る間隔（分）
    outlook_conservative_pct: float = 30.0  # 資産の見通しの下限: プラスの項目はこの%控えめ、マイナスの項目はこの%厳しめ
    outlook_min_hours: float = 24.0         # 始めてからこの時間未満は見通しを出さず「データ不足」（2026-09-29 オーナー指示）


@dataclass(frozen=True)
class EvaluationSettings:
    """2週間の評価（M5d。SPEC 11章）。config.yaml の evaluation から読む。合格の基準は 2026-09-29 オーナー決定。"""
    days: int = 14                          # 評価の期間（日）
    min_coverage_pct: float = 95.0          # データの集まり具合がこの%以上
    day_gap_pct: float = 30.0               # 1日の純損益の差が予測の ±この% 以内なら、その日は満たす
    day_gap_capital_pct: float = 0.1        #   または、差が総資産のこの% 以内なら満たす
    pass_days_pct: float = 70.0             # 満たす日が評価日数のこの%以上
    block_new_practice: bool = True         # 評価の間は新しい練習を始めない（2026-09-30 オーナー決定①）
    # 参考の練習（2026-10-01 オーナー提案【3】案B）: 合否に使わない練習を、評価の間も並べて動かす
    reference_max_open: int = 3             # 同時に持てる参考の建玉の数
    reference_outside_limits: bool = True   # 参考の建玉は、合計と会場ごとの上限（limits）の計算に入れない（1つの金額と1日の件数は守る）
    # 週ごとの見込み（2026-10-01 オーナー提案【2】3）: 木曜の切り替えのあとは、その週の見込みで合否を出す
    weekly_prediction: bool = True


def _evaluation(raw: dict[str, Any]) -> EvaluationSettings:
    e = raw.get("evaluation") or {}
    d = EvaluationSettings()
    out = EvaluationSettings(
        days=int(e.get("days", (raw.get("review") or {}).get("evaluation_days", d.days))),
        **{k: float(e.get(k, getattr(d, k))) for k in ("min_coverage_pct", "day_gap_pct", "day_gap_capital_pct",
                                                        "pass_days_pct")},
        block_new_practice=bool(e.get("block_new_practice", d.block_new_practice)),
        reference_max_open=int(e.get("reference_max_open", d.reference_max_open)),
        reference_outside_limits=bool(e.get("reference_outside_limits", d.reference_outside_limits)),
        weekly_prediction=bool(e.get("weekly_prediction", d.weekly_prediction)))
    if out.days <= 0:
        raise ConfigError("evaluation.days は1以上にしてください。")
    return out


def _review(raw: dict[str, Any]) -> ReviewSettings:
    r = raw.get("review") or {}
    d = ReviewSettings()
    out = ReviewSettings(every_minutes=int(r.get("every_minutes", d.every_minutes)),
                         outlook_conservative_pct=float(r.get("outlook_conservative_pct", d.outlook_conservative_pct)),
                         outlook_min_hours=float(r.get("outlook_min_hours", d.outlook_min_hours)))
    if out.every_minutes <= 0 or 60 % out.every_minutes and out.every_minutes % 60:
        raise ConfigError("review.every_minutes は 60 を割り切れる数か、60 の倍数（30 など）にしてください。")
    return out


@dataclass(frozen=True)
class HedgeVenueSettings:
    """ヘッジ先（SPEC 5.2.1章）。config.yaml の hedge_venues。読み取りだけに使う。"""
    hedge_id: str
    account_address: str | None = None      # 担保の残高を読むアドレス（公開情報。秘密鍵ではない）


def _hedge_venues(raw: dict[str, Any]) -> tuple[HedgeVenueSettings, ...]:
    hv = raw.get("hedge_venues")
    if hv is None:
        hv = {"lighter": {}}
    out = []
    for hid, v in (hv or {}).items():
        v = v or {}
        if v.get("enabled", True) is False:
            continue
        # .env の <ID>_ACCOUNT_ADDRESS（例: LIGHTER_ACCOUNT_ADDRESS）があればそちらを使う
        addr = os.environ.get(f"{hid.upper()}_ACCOUNT_ADDRESS") or v.get("account_address") or None
        if addr is not None:
            addr = str(addr).strip() or None
        if addr and not (addr.startswith("0x") and len(addr) == 42):
            raise ConfigError(f"hedge_venues.{hid}.account_address は 0x で始まる42文字のアドレスにしてください。")
        out.append(HedgeVenueSettings(str(hid), addr))
    return tuple(out)


@dataclass(frozen=True)
class DiscoveryChain:
    """候補の一覧で見るチェーン。名前はサイトごとに違うので、それぞれ書く。"""
    name: str                               # DefiLlama のチェーン名（例: "Robinhood Chain"）
    coins: str | None = None                # DefiLlama のトークン価格でのチェーン名（例: robinhood）
    geckoterminal: str | None = None        # GeckoTerminal のネットワーク名（例: robinhood）。空なら読まない


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class DiscoverySettings:
    """候補の会場の一覧（週1回。SPEC 5.2.2章。2026-09-29 オーナー依頼 E1）。config.yaml の discovery。"""
    weekday: str = "mon"                    # 毎週この曜日に（日本時間）
    hour_jst: int = 9
    minute_jst: int = 0
    chains: tuple[DiscoveryChain, ...] = (
        DiscoveryChain("Robinhood Chain", "robinhood", "robinhood"),
        DiscoveryChain("Base", "base"),
        DiscoveryChain("Arbitrum", "arbitrum"),
        DiscoveryChain("Hyperliquid L1", "hyperliquid"),
        DiscoveryChain("Avalanche", "avax"),
    )
    min_tvl_usd: float = 300_000.0          # そのチェーンでの預かり額がこれ以上の会場だけ
    pool_min_tvl_usd: float = 20_000.0      # プールはこれ以上の預かり額のものだけ出す
    new_days: int = 90                      # 掲載からこの日数以内なら、ボーナスがなくても「新しい会場」として出す
    new_pool_days: int = 30                 # ボーナスの記録がこの日数以内のプールを「新しくボーナスが出始めたプール」に出す
    refresh_min_minutes: int = 30           # 「今すぐ更新」は前回の開始からこの分数あける


def _discovery(raw: dict[str, Any]) -> DiscoverySettings:
    r = raw.get("discovery") or {}
    d = DiscoverySettings()
    at = str(r.get("time_jst", f"{d.hour_jst:02d}:{d.minute_jst:02d}"))
    try:
        hh, mm = (int(x) for x in at.split(":"))
    except ValueError:
        raise ConfigError("discovery.time_jst は \"09:00\" のように書いてください。") from None
    weekday = str(r.get("weekday", d.weekday)).lower()[:3]
    if weekday not in _WEEKDAYS:
        raise ConfigError("discovery.weekday は mon〜sun のどれかにしてください。")
    chains = d.chains
    if r.get("chains") is not None:
        chains = tuple(DiscoveryChain(str(c["name"]), c.get("coins") or None, c.get("geckoterminal") or None)
                       for c in r["chains"] or [])
    return DiscoverySettings(
        weekday=weekday, hour_jst=hh, minute_jst=mm, chains=chains,
        min_tvl_usd=float(r.get("min_tvl_usd", d.min_tvl_usd)),
        pool_min_tvl_usd=float(r.get("pool_min_tvl_usd", d.pool_min_tvl_usd)),
        new_days=int(r.get("new_days", d.new_days)),
        new_pool_days=int(r.get("new_pool_days", d.new_pool_days)),
        refresh_min_minutes=int(r.get("refresh_min_minutes", d.refresh_min_minutes)),
    )


@dataclass(frozen=True)
class ObserveVenueSettings:
    """観察だけの会場（practice: false。M6 の Alandale）の読み取りを休む条件（SPEC 5.1章。2026-09-30 オーナー条件）。"""
    defer_below_coverage_pct: float = 97.0  # 練習・評価の会場の収集率がこの%未満なら休む（評価の合格ライン95%より少し上）
    rate_limit_quiet_minutes: float = 30.0  # この分数の中に 429 があれば休む
    max_start_delay_minutes: float = 5.0    # 予定時刻からこの分数を過ぎていたら、その回は休む


def _observe_venues(raw: dict[str, Any]) -> ObserveVenueSettings:
    o = raw.get("observe_venues") or {}
    d = ObserveVenueSettings()
    out = ObserveVenueSettings(**{k: float(o.get(k, getattr(d, k))) for k in
                                  ("defer_below_coverage_pct", "rate_limit_quiet_minutes", "max_start_delay_minutes")})
    if not 0 <= out.defer_below_coverage_pct <= 100:
        raise ConfigError("observe_venues.defer_below_coverage_pct は0〜100にしてください。")
    return out


@dataclass(frozen=True)
class Config:
    mode: str
    database_path: Path
    snapshot_minutes: int
    rpc: RpcSettings
    venues: tuple[str, ...]
    stale_after_minutes: int
    limits: dict[str, Any] = field(default_factory=dict)
    epoch_fresh_minutes: int = 120          # エポック切り替えからこの分数までは「エポック更新直後」の印を付ける
    reward_drop_alert_pct: float = 30.0     # エポックの途中で報酬の毎秒量がこの%以上減ったら通知
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    review: ReviewSettings = field(default_factory=ReviewSettings)
    evaluation: EvaluationSettings = field(default_factory=EvaluationSettings)
    hedge_venues: tuple[HedgeVenueSettings, ...] = (HedgeVenueSettings("lighter"),)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    observe_venues: ObserveVenueSettings = field(default_factory=ObserveVenueSettings)
    root: Path = REPO_ROOT


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    path = path or REPO_ROOT / "config.yaml"
    env = os.environ if env is None else env
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = path.resolve().parent

    mode = raw.get("mode")
    if mode not in ALLOWED_MODES:
        raise ConfigError(
            f"config.yaml の mode={mode!r} は使えません。使えるのは {', '.join(ALLOWED_MODES)} だけです。"
        )

    snapshot_minutes = int(raw.get("schedule", {}).get("snapshot_minutes", 15))
    if snapshot_minutes <= 0:
        raise ConfigError("schedule.snapshot_minutes は1以上にしてください。")

    rpc_raw = raw.get("rpc", {}) or {}
    extra = list(rpc_raw.get("extra_urls") or [])
    extra += [u.strip() for u in env.get("EXTRA_RPC_URLS", "").split(",") if u.strip()]
    rpc = RpcSettings(
        max_retries=int(rpc_raw.get("max_retries", 3)),
        backoff_seconds=float(rpc_raw.get("backoff_seconds", 1.0)),
        timeout_seconds=float(rpc_raw.get("timeout_seconds", 15)),
        extra_urls=tuple(extra),
        latest_cache_seconds=float(rpc_raw.get("latest_cache_seconds", 5)),
    )

    db_path = Path(raw.get("database", {}).get("path", "data/farm_radar.sqlite3"))
    if not db_path.is_absolute():
        db_path = root / db_path

    return Config(
        mode=mode,
        database_path=db_path,
        snapshot_minutes=snapshot_minutes,
        rpc=rpc,
        venues=tuple(raw.get("venues") or ()),
        stale_after_minutes=int(raw.get("stale_after_minutes", 45)),
        limits=dict(raw.get("limits") or {}),
        epoch_fresh_minutes=int((raw.get("rewards") or {}).get("epoch_fresh_minutes", 120)),
        reward_drop_alert_pct=float((raw.get("alerts") or {}).get("reward_rate_drop_pct", 30)),
        scoring=_scoring(raw),
        notify=_notify(raw),
        risk=_risk(raw),
        review=_review(raw),
        evaluation=_evaluation(raw),
        hedge_venues=_hedge_venues(raw),
        discovery=_discovery(raw),
        observe_venues=_observe_venues(raw),
        root=root,
    )


def load_venue(venue_id: str, root: Path = REPO_ROOT) -> dict[str, Any]:
    path = root / "venues" / f"{venue_id}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("id") != venue_id:
        raise ConfigError(f"{path} の id が {venue_id} と一致しません。")
    return data


def mechanic_value(venue: dict[str, Any], name: str, default: Any = None) -> Any:
    """会場の仕組み（mechanics）の値。確認済みか、未確認のままオーナーが承認したものだけ。それ以外は default。"""
    m = (venue.get("mechanics") or {}).get(name) or {}
    if "value" not in m or (m.get("unverified", True) and not m.get("owner_acknowledged")):
        return default
    return m["value"]


def practice_allowed(venue: dict[str, Any]) -> bool:
    """この会場で練習（と評価）ができるか。practice: false の会場は観察だけ（M6 の Alandale）。"""
    return venue.get("practice", True) is not False


def contract_address(venue: dict[str, Any], name: str) -> str | None:
    """確認済みのアドレスだけを返す。未確認（unverified: true）なら None。"""
    entry = (venue.get("contracts") or {}).get(name) or {}
    if entry.get("unverified", True) or not entry.get("address"):
        return None
    return entry["address"]
