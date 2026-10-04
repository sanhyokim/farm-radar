import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { useApi, type PlanItem, type Reports } from "../api";
import { jst, jstDay, untilText } from "../format";
import { GLOSSARY } from "../glossary";
import { Icon } from "../icons";
import { Card, Fold, Folds, Note, PageHead, Pill } from "../ui";

// 学ぶ（SPEC 7.5章）。用語の説明から「学ぶで詳しく見る」で開いたときは、その用語まで動かして目立たせる（オーナー依頼 16）
export default function Learn() {
  const { data } = useApi<Reports>("/api/reports", 300_000);
  const { hash } = useLocation();
  const target = hash.startsWith("#term-") ? hash.slice(1) : null;
  useEffect(() => {
    if (!hash) return;
    const t = window.setTimeout(() => document.getElementById(hash.slice(1))?.scrollIntoView({ block: "center" }), 150);
    return () => window.clearTimeout(t);
  }, [hash, data]);
  return (
    <>
      <PageHead title="学ぶ" />
      <div className="grid items-start gap-4 lg:grid-cols-2 lg:gap-6">
        <div className="flex flex-col gap-4 lg:gap-6">
          <TrialLink />
          <PlansCard />
          <Card title="今日の学び">
            {data?.learning.length ? data.learning.slice(0, 3).map((n) => (
              <div key={n.ts} className="flex flex-col gap-1">
                <div className="cap">{jst(n.ts)}</div>
                <div className="bold">{n.title}</div>
                <p className="sec">{n.body_ja}</p>
              </div>
            )) : <p className="cap">毎朝のレポートといっしょに、1日1つ作ります。</p>}
          </Card>
          <Folds>
            <div className="flex items-center justify-between gap-2 px-6 pt-6 pb-2">
              <span className="label">毎朝のレポート</span>
              {data && (data.telegram.configured ? <Pill>Telegram 送信中</Pill> : <Pill tone="y">Telegram 未設定</Pill>)}
            </div>
            {data?.reports.length ? data.reports.map((r, i) => (
              <Fold key={r.day} title={<span>{r.day} <span className="cap">{r.sent_at ? "送信済み" : "未送信"}</span></span>} open={i === 0}>
                <pre className="font-sans whitespace-pre-wrap break-words text-sec">{r.body_ja}</pre>
              </Fold>
            )) : <p className="cap px-6 pb-6">まだありません。毎朝 {data?.schedule_jst ?? "08:00"}（日本時間）に作ります。</p>}
            {data && !data.telegram.configured && <p className="cap px-6 pb-6">Telegram の設定（.env）がまだなので、レポートはここに残すだけで送っていません。</p>}
          </Folds>
          <FaqCard />
        </div>
        <section id="glossary" className="card flex flex-col gap-4 p-6">
          <h2 className="label">用語集</h2>
          <dl className="flex flex-col gap-2">
            {GLOSSARY.map((g, i) => {
              const id = `term-${i}`;
              const on = target === id;
              return (
                <div key={g.term} id={id} className="rounded-2xl p-4" style={{ background: on ? "var(--inset)" : undefined, outline: on ? "1px solid rgba(255,255,255,0.16)" : undefined }}>
                  <dt className="bold">{g.term}</dt>
                  <dd className="sec">{g.text}</dd>
                </div>
              );
            })}
          </dl>
        </section>
      </div>
    </>
  );
}

/** N5d「試す」の結果の画面への入り口 */
function TrialLink() {
  return (
    <Link to="/learn/trial" className="card flex items-center gap-4 p-6">
      <Icon name="flask" />
      <div className="min-w-0 flex-1">
        <div className="bold">試すの結果</div>
        <div className="cap">見込みと実際を6つの項目で比べています（確認できた・要注意・記録中）</div>
      </div>
      <Icon name="right" />
    </Link>
  );
}

type Faq = { items: { q: string; paragraphs: { text: string; analogy: boolean }[] }[] };

/** よくある質問（docs/faq.md。2026-09-29 オーナー追加。ファイルに書き足すと増える） */
function FaqCard() {
  const { data } = useApi<Faq>("/api/faq", 0);
  if (!data || data.items.length === 0) return null;
  return (
    <Folds>
      <div className="px-6 pt-6 pb-2"><span className="label">よくある質問</span></div>
      {data.items.map((it) => (
        <Fold key={it.q} title={<span className="text-ink">Q. {it.q}</span>}>
          {it.paragraphs.map((p, j) => <FaqPara key={j} text={p.text} analogy={p.analogy} />)}
        </Fold>
      ))}
    </Folds>
  );
}

function FaqPara({ text, analogy }: { text: string; analogy: boolean }) {
  const lines = text.split("\n");
  const items = lines.filter((l) => l.startsWith("- "));
  const head = lines.filter((l) => !l.startsWith("- "));
  return (
    <div className={analogy ? "inset p-4" : ""}>
      {head.map((l, k) => <p key={k} className="sec">{l}</p>)}
      {items.length > 0 && <ul className="mt-1 flex list-disc flex-col gap-1 pl-5 sec">{items.map((l, k) => <li key={k}>{l.slice(2)}</li>)}</ul>}
    </div>
  );
}

/** 予定とメモ（docs/plans.yaml。日付のあるものは期限の7日前から目立たせる。2026-09-30 オーナー追加） */
function PlansCard() {
  const { data } = useApi<{ items: PlanItem[]; soon_days: number }>("/api/plans", 300_000);
  if (!data || data.items.length === 0) return null;
  const groups = [...new Set(data.items.map((i) => i.group))];
  return (
    <section id="plans" className="card flex flex-col gap-4 p-6">
      <h2 className="label">予定とメモ</h2>
      {groups.map((g) => (
        <div key={g} className="flex flex-col gap-2">
          <div className="cap">{g}</div>
          {data.items.filter((i) => i.group === g).map((i) => <PlanRow key={i.key} p={i} />)}
        </div>
      ))}
      <Note>期限の{data.soon_days}日前から目立たせます。中身は docs/plans.yaml（PROGRESS.md と同じ）です。</Note>
    </section>
  );
}

function PlanRow({ p }: { p: PlanItem }) {
  const soon = p.state === "soon";
  const past = p.state === "past";
  return (
    <div className="inset flex flex-col gap-1 p-4" style={soon ? { outline: "1px solid rgba(251,191,36,0.4)" } : undefined}>
      <div className="flex items-start justify-between gap-2">
        <span className={`bold ${past ? "text-cap" : ""}`}>{p.title}</span>
        {p.due && (
          <span className="cap shrink-0 text-right" style={soon ? { color: "var(--y)" } : undefined}>
            {soon && <Icon name="clock" size={12} className="mr-1 inline" />}{jstDay(p.due)}<br />{past ? "期限を過ぎました" : untilText(p.due)}
          </span>
        )}
      </div>
      <p className="sec">{p.text}</p>
      {p.source && <p className="cap">{p.source}</p>}
    </div>
  );
}
