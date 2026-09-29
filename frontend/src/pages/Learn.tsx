import { GLOSSARY } from "../glossary";
import { Card } from "../ui";

export default function Learn() {
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100">学ぶ</h1>
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
      <Card title="日次レポートの履歴">
        <p className="text-sm text-slate-400">
          「今日何が起きたか」「なぜ判定が変わったか」「今日の学び」を毎朝まとめます。M4 で作ります。
        </p>
      </Card>
    </div>
  );
}
