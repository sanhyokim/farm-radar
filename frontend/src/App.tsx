import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { PulseProvider, TermProvider, usePulse } from "./ui";
import { Icon, type IconName } from "./icons";
import { hm } from "./format";
import Home from "./pages/Home";
import Venues from "./pages/Venues";
import Pools from "./pages/Pools";
import PoolDetail from "./pages/PoolDetail";
import Practice from "./pages/Practice";
import PracticeDetail from "./pages/PracticeDetail";
import { TimelinePage } from "./pages/PaperExtras";
import Learn from "./pages/Learn";
import Opportunities from "./pages/Opportunities";

const TABS: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "ホーム", icon: "home" },
  { to: "/opportunities", label: "機会", icon: "flag" },
  { to: "/venues", label: "会場", icon: "venue" },
  { to: "/pools", label: "プール", icon: "pools" },
  { to: "/practice", label: "練習", icon: "flask" },
  { to: "/learn", label: "学ぶ", icon: "learn" },
];

export default function App() {
  const { pathname } = useLocation();
  // プールの詳しい画面（スマホ）は、下のタブの代わりにボタンを浮かべる
  const detail = /^\/pools\/.+/.test(pathname);
  useEffect(() => { if (!window.location.hash) window.scrollTo(0, 0); }, [pathname]);
  return (
    <PulseProvider>
      <TermProvider>
        <SideNav />
        <main className={`mx-auto flex max-w-xl flex-col gap-4 px-4 pt-6 lg:ml-64 lg:max-w-none lg:px-8 lg:pt-8 lg:pb-8 ${detail ? "pb-32" : "pb-tab"}`}>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/opportunities" element={<Opportunities />} />
            <Route path="/venues" element={<Venues />} />
            <Route path="/pools" element={<Pools />} />
            <Route path="/pools/:id" element={<PoolDetail />} />
            <Route path="/practice" element={<Practice />} />
            <Route path="/practice/timeline" element={<TimelinePage />} />
            <Route path="/practice/:id" element={<PracticeDetail />} />
            <Route path="/learn" element={<Learn />} />
            <Route path="*" element={<Home />} />
          </Routes>
        </main>
        {!detail && <TabBar />}
      </TermProvider>
    </PulseProvider>
  );
}

/** スマホ: 下に浮かぶタブ（パソコンでは左のメニューに変わる。オーナー依頼 33）。N2b で「機会」を足して6つ */
function TabBar() {
  return (
    <nav aria-label="メニュー" className="glass tabbar fixed inset-x-4 z-40 mx-auto grid h-16 max-w-lg grid-cols-6 rounded-[32px] p-2 lg:hidden">
      {TABS.map((t) => (
        <NavLink key={t.to} to={t.to} end={t.to === "/"}
          className={({ isActive }) => `flex flex-col items-center justify-center rounded-3xl ${isActive ? "bg-white/10 text-ink" : "text-cap"}`}>
          <Icon name={t.icon} />
          <span className="text-[12px] leading-4">{t.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

/** パソコン: 左のメニュー。下に収集の状態と計算の時刻 */
function SideNav() {
  const pulse = usePulse();
  return (
    <nav aria-label="メニュー" className="card fixed top-4 bottom-4 left-4 z-40 hidden w-56 flex-col gap-2 px-4 py-6 lg:flex">
      <div className="t20 px-4 pb-4">Farm Radar</div>
      {TABS.map((t) => (
        <NavLink key={t.to} to={t.to} end={t.to === "/"}
          className={({ isActive }) => `flex min-h-12 items-center gap-4 rounded-full px-4 ${isActive ? "bg-white/10 font-semibold text-ink" : "text-cap hover:text-sec"}`}>
          <Icon name={t.icon} /><span>{t.label}</span>
        </NavLink>
      ))}
      <div className="inset mt-auto flex flex-col gap-2 p-4">
        {pulse == null ? <span className="cap">読み込み中…</span> : (
          <>
            {pulse.chain_reads === false ? (
              <span className="cap flex items-center gap-2"><Icon name="download" size={16} />一覧の保存だけ（チェーンは読まない版）</span>
            ) : (
              <span className="cap flex items-center gap-2" style={{ color: pulse.stale ? "var(--y)" : "var(--sec)" }}>
                <Icon name={pulse.stale ? "alert" : "check"} size={16} />{pulse.stale ? "収集が止まっています" : "収集は正常"}
              </span>
            )}
            <span className="cap">{pulse.snapshot_minutes}分ごと · {hm(pulse.scored_at)} に計算</span>
            <span className="cap">{pulse.mode === "paper" ? "練習モード" : "観察モード"}</span>
          </>
        )}
      </div>
    </nav>
  );
}
