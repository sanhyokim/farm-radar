import { useState } from "react";
import { useApi, type Reports } from "../api";
import { jst } from "../format";
import { GLOSSARY } from "../glossary";
import { Badge, Card, Note } from "../ui";

export default function Learn() {
  const { data } = useApi<Reports>("/api/reports");
  const [open, setOpen] = useState(0);
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100">学ぶ</h1>

      <Card title="今日の学び">
        {data?.learning.length ? (
          <div className="space-y-3">
            {data.learning.slice(0, 3).map((n) => (
              <div key={n.ts}>
                <div className="text-xs text-slate-500">{jst(n.ts)}</div>
                <div className="font-semibold text-slate-100">{n.title}</div>
                <p className="text-sm leading-relaxed text-slate-300">{n.body_ja}</p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-400">毎朝のレポートといっしょに、1日1つ作ります。</p>
        )}
      </Card>

      <Card
        title="毎朝のレポート"
        right={data && (data.telegram.configured
          ? <Badge tone="emerald">Telegram 送信中</Badge>
          : <Badge tone="amber">Telegram 未設定</Badge>)}
      >
        {data?.reports.length ? (
          <div className="space-y-2">
            {data.reports.map((r, i) => (
              <div key={r.day} className="rounded-xl bg-slate-800/40">
                <button className="flex w-full items-center justify-between p-2 text-left text-sm"
                        onClick={() => setOpen(open === i ? -1 : i)}>
                  <span className="font-semibold text-slate-100">{r.day}</span>
                  <span className="text-xs text-slate-400">{r.sent_at ? "送信済み" : "未送信"} {open === i ? "▲" : "▼"}</span>
                </button>
                {open === i && (
                  <pre className="whitespace-pre-wrap break-words px-2 pb-2 font-sans text-sm leading-relaxed text-slate-300">{r.body_ja}</pre>
                )}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-400">まだありません。毎朝 {data?.schedule_jst ?? "08:00"}（日本時間）に作ります。</p>
        )}
        {data && !data.telegram.configured && (
          <Note>Telegram の設定（.env）がまだなので、レポートはここに残すだけで送っていません。</Note>
        )}
      </Card>

      <FaqCard />

      <Card title="用語集">
        <dl className="space-y-3">
          {GLOSSARY.map((g) => (
            <div key={g.term}>
              <dt className="font-semibold text-slate-100">{g.term}</dt>
              <dd className="text-sm leading-relaxed text-slate-300">{g.text}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </div>
  );
}

type Faq = { items: { q: string; paragraphs: { text: string; analogy: boolean }[] }[] };

/** よくある質問（docs/faq.md。2026-09-29 オーナー追加。ファイルに書き足すと増える） */
function FaqCard() {
  const { data } = useApi<Faq>("/api/faq");
  const [open, setOpen] = useState<number | null>(null);
  if (!data || data.items.length === 0) return null;
  return (
    <Card title="よくある質問">
      <div className="space-y-2">
        {data.items.map((it, i) => (
          <div key={it.q} className="rounded-xl bg-slate-800/40">
            <button className="flex w-full items-start justify-between gap-2 p-2 text-left text-sm"
                    onClick={() => setOpen(open === i ? null : i)}>
              <span className="font-semibold text-slate-100">Q. {it.q}</span>
              <span className="shrink-0 text-xs text-slate-400">{open === i ? "▲" : "▼"}</span>
            </button>
            {open === i && (
              <div className="space-y-2 px-2 pb-3 text-sm leading-relaxed text-slate-300">
                {it.paragraphs.map((p, j) => <FaqPara key={j} text={p.text} analogy={p.analogy} />)}
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

function FaqPara({ text, analogy }: { text: string; analogy: boolean }) {
  const lines = text.split("\n");
  const items = lines.filter((l) => l.startsWith("- "));
  const head = lines.filter((l) => !l.startsWith("- "));
  return (
    <div className={analogy ? "rounded-lg bg-sky-500/10 p-2 text-sky-100" : ""}>
      {head.map((l, k) => <p key={k}>{l}</p>)}
      {items.length > 0 && (
        <ul className="mt-1 list-disc space-y-0.5 pl-5">{items.map((l, k) => <li key={k}>{l.slice(2)}</li>)}</ul>
      )}
    </div>
  );
}
