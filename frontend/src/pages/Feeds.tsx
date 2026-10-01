import { useApi, type FeedNew, type FeedSource, type FeedsStatus } from "../api";
import { jst, jstDay, untilText, usd } from "../format";
import { Icon } from "../icons";
import { Fold, Line, Pill } from "../ui";

// 一覧の保存（N2a。作り直し（渡り鳥）SPEC 13.4）。Merkl・DefiLlama・送金サービス・Aero のお知らせを保存できているか
export function FeedsCard() {
  const { data } = useApi<FeedsStatus>("/api/feeds/status", 60_000);
  if (!data || !data.enabled) return null;
  const okCount = data.sources.filter((s) => s.status === "ok" && !s.late).length;
  const newArticle = data.aero.articles.find((a) => a.new);
  const opps = data.new.filter((n) => n.source === "merkl_opportunities");
  const others = data.new.filter((n) => n.source !== "merkl_opportunities" && n.source !== "aero_articles");
  return (
    <section className="card flex flex-col gap-4 p-6">
      <div className="flex items-center justify-between gap-2">
        <span className="label flex items-center gap-2"><Icon name="download" size={16} /><span>一覧の保存</span></span>
        <Pill tone={data.problem ? "y" : "g"} icon={data.problem ? "alert" : "check"}>
          {data.problem ? "一部が読めていません" : "保存中"}
        </Pill>
      </div>
      <p className="sec">{data.text}</p>
      {data.chain_reads === false && (
        <p className="cap">この版はチェーンを読まず、一覧の保存だけを動かしています（今までの版はポート 18000 の画面です）。</p>
      )}

      <div className="inset flex flex-col gap-2 p-4">
        <Line k="Aero の開始（日本時間）" v={jstDay(data.aero.start.utc)} note={untilText(data.aero.start.utc)} strong />
        {newArticle ? (
          <a href={newArticle.url} target="_blank" rel="noreferrer" className="cap flex items-center gap-2" style={{ color: "var(--y)" }}>
            <Icon name="alert" size={16} /><span>新しいお知らせ: {newArticle.title}（{newArticle.date ?? "日付なし"}）</span>
          </a>
        ) : (
          <span className="cap">公式のお知らせは1時間ごとに確かめています。新しい記事が出たらここに出ます。</span>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <span className="cap">新しく出てきたボーナス（Merkl。直近48時間）</span>
        {opps.length === 0 ? <span className="cap">まだありません（保存を始めた最初の回の分は数えません）。</span>
          : opps.slice(0, 8).map((n) => <NewOpp key={n.key} n={n} />)}
        {opps.length > 8 && <span className="cap">ほか {opps.length - 8}件</span>}
      </div>

      <Fold title={`一覧ごとの状態（${okCount}/${data.sources.length} が予定どおり）`}>
        {data.sources.map((s) => <SourceLine key={s.id} s={s} />)}
        {others.length > 0 && (
          <div className="flex flex-col gap-1">
            <span className="cap">新しく出てきた会場・チェーン（直近48時間）</span>
            {others.slice(0, 12).map((n) => (
              <span key={`${n.source}:${n.key}`} className="cap">{n.label}: {n.name ?? n.key}{n.chain ? `（${n.chain}）` : ""}</span>
            ))}
          </div>
        )}
        <span className="cap">
          保存の量 {mb(data.disk_bytes)} · 24時間の回数制限（429） {data.rate_limited_24h ?? 0}回 · 保存を始めた時刻 {jst(data.started_at)}
        </span>
        <span className="cap">読むだけで、チェーンの読み取り口は使いません。数字はまだ計算に使っていません（次の段階で使います）。</span>
      </Fold>
    </section>
  );
}

function NewOpp({ n }: { n: FeedNew }) {
  const i = n.info;
  const parts = [n.chain, i.protocol, i.status === "SOON" ? "予定" : null].filter(Boolean).join(" · ");
  return (
    <div className="inset flex flex-col gap-1 p-4">
      <span className="truncate">{n.name ?? n.key}</span>
      <span className="cap">{parts}</span>
      <span className="cap num">
        表示の年利 {i.apr == null ? "—" : `${i.apr.toFixed(1)}%`} · 預かり額 {usd(i.tvl ?? null, 0)} · 1日の配布 {usd(i.daily_rewards ?? null, 0)}
      </span>
    </div>
  );
}

function SourceLine({ s }: { s: FeedSource }) {
  const bad = s.status !== "ok" || s.late;
  const v = s.status === "ok" ? `${(s.items ?? 0).toLocaleString("en-US")}件${s.new_today ? ` · 今日+${s.new_today}` : ""}` : s.status_ja;
  return (
    <Line k={s.label} v={<span style={{ color: bad ? "var(--y)" : undefined }}>{s.late && s.status === "ok" ? "遅れています" : v}</span>}
      note={`${s.cadence_ja} · 最後に保存 ${jst(s.last_ok_at)}`} />
  );
}

const mb = (b: number | undefined) => (b == null ? "—" : b >= 1e9 ? `${(b / 1e9).toFixed(2)}GB` : `${(b / 1e6).toFixed(0)}MB`);
