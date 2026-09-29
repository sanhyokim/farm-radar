import { Card, Term } from "../ui";

export default function Practice() {
  return (
    <div className="space-y-3">
      <h1 className="text-lg font-bold text-slate-100">練習（ペーパートレード）</h1>
      <Card>
        <p className="leading-relaxed text-slate-200">
          仮想のお金（$1,000）でプールを試す画面です。<b>M5</b> で作ります。
        </p>
        <p className="mt-2 text-sm leading-relaxed text-slate-400">
          ここでは実際の<Term k="建玉" />の損益を、プール詳細と同じ6つの区分で毎時表示し、実現・未実現、予測とのずれ、
          タイムライン、台帳も出す予定です。今はプール詳細で「予測」を見られます。
        </p>
      </Card>
    </div>
  );
}
