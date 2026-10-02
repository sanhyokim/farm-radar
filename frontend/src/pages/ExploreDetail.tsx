import { Link, useParams, useSearchParams } from "react-router-dom";
import { useApi, type OppCampaign, type OppDetailResp, type Safety } from "../api";
import { bigUsd, jst, usd } from "../format";
import { Icon } from "../icons";
import { Card, Fold, Folds, Line, Loading, Note, PageHead, Pill, Segmented, Term } from "../ui";
import { Breakdown, GuessPill, KIND, SafetyPill, Side, VenueMatchPill, apr, days, isGuess, isUncertainVenue } from "./opp";

/**
 * 入れる先の詳しい画面（N2c）: 計算の内訳、保険あり・なし、印、この会場の安全度、「$1,000 を試す」。
 * 読み取りと計算だけ。「試す」は練習（お金は動かない）。
 */
export default function ExploreDetail() {
  const { key = "" } = useParams();
  const [sp, setSp] = useSearchParams();
  const amount = sp.get("amount") ?? "1000";
  const { data, error } = useApi<OppDetailResp>(`/api/opportunities/${encodeURIComponent(key)}?amount=${amount}`, 5 * 60_000);
  const backQ = new URLSearchParams(sp);
  const back = { to: `/explore${backQ.toString() ? `?${backQ.toString()}` : ""}`, label: "探す" };
  const head = <PageHead title={data?.item.name ?? "入れる先"} at={data?.computed_at} back={back} />;
  if (!data) return <>{head}<Loading error={error} /></>;
  const o = data.item;
  const b = o.best;
  const row = o.calc[amount] ?? null;
  const warn = o.flags.filter((f) => f.level === "warn");
  const excl = o.flags.filter((f) => f.level === "exclude");
  const info = o.flags.filter((f) => f.level === "info");
  const receipt = o.flags.find((f) => f.code === "RECEIPT");
  const noHedgeMarket = o.flags.some((f) => f.code === "NO_HEDGE");
  const amounts = data.amounts.map((a) => ({ key: String(a), label: usd(a, 0) }));
  const setAmount = (v: string) => { const n = new URLSearchParams(sp); n.set("amount", v); setSp(n, { replace: true }); };

  return (
    <>
      {head}
      <section className="card flex flex-col gap-4 p-6">
        <div className="flex flex-col gap-1">
          <span className="bold" style={{ overflowWrap: "anywhere" }}>{o.name ?? o.key}</span>
          <span className="cap">{[o.chain_name, o.venue_name ?? o.venue, KIND[o.kind]].filter(Boolean).join(" · ")}</span>
        </div>
        <div className="flex flex-wrap gap-2">
          {o.excluded ? <Pill tone="r" icon="alert">外した</Pill>
            : o.above_target ? <Pill tone="g" icon="check">狙い以上</Pill> : <Pill>狙いより低い</Pill>}
          <SafetyPill s={o.safety} />
          {isGuess(o) && <GuessPill />}
          {isUncertainVenue(o) && <VenueMatchPill />}
          {o.new_pool && <Pill tone="y" icon="info">始まったばかり</Pill>}
        </div>
        <div className="flex items-end justify-between gap-4">
          <div className="flex flex-col">
            <span className="cap"><Term k="控えめの見込み">控えめの見込み</Term>（{b?.hedge ? "保険あり" : "保険なし"}・年利）</span>
            <span className="t32 num">{apr(b?.apr_pct)}</span>
          </div>
          <div className="flex flex-col items-end">
            <span className="cap">1日に残る額</span>
            <span className="num">{usd(b?.net_after_move ?? null)}</span>
          </div>
        </div>
        <Segmented options={amounts} value={amount} onChange={setAmount} />
        {row && (
          <div className="grid grid-cols-2 gap-2">
            <Side title="保険なし" c={row} k="no_hedge" />
            <Side title="保険あり" c={row} k="hedge"
              empty={noHedgeMarket ? "保険の売り場（Lighter）がない" : "値動きしないコインだけなので、保険はいらない"} />
          </div>
        )}
      </section>

      <TryCard data={data} />

      <Card title={<span className="flex items-center gap-2"><Icon name="shield" size={16} />安全度（仮）</span>}>
        <SafetyBlock title="この入れる先" s={o.safety} />
        <SafetyBlock title={`この会場（${o.venue_name ?? o.venue}）`} s={o.venue_safety} />
        <Note>
          仮の3段階です。N4 で「危なさの点数」ができたら置きかえます。会場の情報は Merkl が載せているもの（もとは DefiLlama）で、
          名前の似た別の会場のものが混ざることがあります。
        </Note>
        <Link to="/guard" className="cap flex items-center gap-2 text-sec"><Icon name="right" size={16} />守る（置いている額と上限）を見る</Link>
      </Card>

      <Card title="数字">
        <Line k="表示のボーナスの年利" v={apr(o.shown_apr_pct)} note="割り引く前。預け先の利息は入っていない" />
        <Line k={<Term k="TVL">預かり額</Term>} v={bigUsd(o.tvl_usd)} />
        <Line k="配る期間の残り" v={days(o.days_left)} note={o.ends_at ? `${jst(o.ends_at)} まで` : undefined} />
        {b && <Line k={<Term k="入る・出る費用">入る・出る費用</Term>} v={usd(b.move_cost)}
          note={b.payback_days == null ? "残る額がマイナスなので取り返せない" : `${days(b.payback_days)}で取り返す`} />}
        {o.cap_usd != null && (
          <Line k="入れてよい上限" v={<span style={{ color: o.over_cap ? "var(--y)" : undefined }}>{bigUsd(o.cap_usd)}</span>}
            note={o.over_cap ? "入れる額が上限より多い（自分のお金で大きく薄まる）" : undefined} />
        )}
      </Card>

      {(o.unprotected.length > 0 || excl.length > 0 || warn.length > 0 || receipt) && (
        <section className="card flex flex-col gap-4 p-6">
          {[...excl, ...warn].map((f) => (
            <span key={f.code + f.text} className="flex items-start gap-2" style={{ color: f.level === "exclude" ? "var(--r)" : "var(--y)" }}>
              <Icon name="alert" size={16} /><span className="cap" style={{ color: "inherit" }}>{f.text}</span>
            </span>
          ))}
          {receipt && <span className="cap flex items-start gap-2"><Icon name="info" size={16} /><span>{receipt.text}</span></span>}
          {o.unprotected.length > 0 && (
            <div className="inset flex flex-col gap-1 p-4">
              <span className="cap flex items-center gap-2" style={{ color: "var(--y)" }}>
                <Icon name="shield" size={16} /><Term k="保険で守れない値動き">保険で守れない値動き</Term>
              </span>
              {o.unprotected.map((u) => <span key={u} className="cap">・{u}</span>)}
            </div>
          )}
        </section>
      )}

      <Folds>
        {b && (
          <Fold title="くわしい内訳（控えめ・1日あたり）" open>
            <Breakdown v={b} />
          </Fold>
        )}
        {data.campaigns.length > 0 && (
          <Fold title={`ボーナスの配り方（${data.campaigns.length}）`}>
            {data.campaigns.map((c) => <CampaignLine key={c.campaign_id} c={c} />)}
          </Fold>
        )}
        {info.filter((f) => !["RECEIPT", "VENUE_MATCH"].includes(f.code)).length > 0 && (
          <Fold title={`印（${info.filter((f) => !["RECEIPT", "VENUE_MATCH"].includes(f.code)).length}）`}>
            {info.filter((f) => !["RECEIPT", "VENUE_MATCH"].includes(f.code)).map((f) => (
              <span key={f.code} className="cap flex items-start gap-2"><Icon name="info" size={16} /><span>{f.text}</span></span>
            ))}
          </Fold>
        )}
      </Folds>

      {o.url && (
        <a href={o.url} target="_blank" rel="noreferrer" className="cap flex items-center gap-2 text-sec">
          <Icon name="link" size={16} />会場のページを開く（見るだけ。入れるかどうかは自分で決める）
        </a>
      )}
    </>
  );
}

function SafetyBlock({ title, s }: { title: string; s: Safety | null }) {
  return (
    <div className="inset flex flex-col gap-2 p-4">
      <div className="flex items-center justify-between gap-2"><span className="sec">{title}</span><SafetyPill s={s} short /></div>
      {s ? s.reasons.map((r) => <span key={r} className="cap">・{r}</span>) : <span className="cap">分かりません。</span>}
    </div>
  );
}

/** 「$1,000 を試す」: 自分で読む会場のプールは今の練習（プールの画面の「試す」）へ。Merkl の入れる先は N6 */
function TryCard({ data }: { data: OppDetailResp }) {
  const p = data.practice;
  return (
    <section className="card flex flex-col gap-2 p-6">
      {p.available && p.pool_id ? (
        <Link to={`/pools/${encodeURIComponent(p.pool_id)}`} className="btn self-start">
          <Icon name="flask" size={16} />$1,000 を試す
        </Link>
      ) : (
        <button type="button" className="btn self-start" disabled><Icon name="flask" size={16} />$1,000 を試す</button>
      )}
      <span className="cap">
        {p.available ? "練習です（お金は動きません）。プールの画面で、幅と保険を確かめてから始めます。" : p.note}
      </span>
    </section>
  );
}

function CampaignLine({ c }: { c: OppCampaign }) {
  const end = c.end_ts ? jst(new Date(c.end_ts * 1000).toISOString()) : "—";
  return (
    <Line k={`${c.reward_symbol ?? "?"}（${c.distribution_method ?? c.distribution_type ?? "?"}）`}
      v={c.daily_rewards != null ? `${usd(c.daily_rewards, 0)}/日` : "—"}
      note={`${end} まで${c.restricted ? "・参加できる人が限られている" : ""}${c.reward_type && c.reward_type !== "TOKEN" ? `・${c.reward_type}` : ""}`} />
  );
}
