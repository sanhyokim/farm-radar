import { NavLink, Route, Routes } from "react-router-dom";
import { TermProvider } from "./ui";
import Home from "./pages/Home";
import Venues from "./pages/Venues";
import Pools from "./pages/Pools";
import PoolDetail from "./pages/PoolDetail";
import Practice from "./pages/Practice";
import Learn from "./pages/Learn";

const TABS = [
  { to: "/", label: "ホーム", icon: "🏠" },
  { to: "/venues", label: "会場", icon: "🏛" },
  { to: "/pools", label: "プール", icon: "💧" },
  { to: "/practice", label: "練習", icon: "🧪" },
  { to: "/learn", label: "学ぶ", icon: "📘" },
];

export default function App() {
  return (
    <TermProvider>
      <main className="pb-safe mx-auto max-w-xl px-3 pt-3">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/venues" element={<Venues />} />
          <Route path="/pools" element={<Pools />} />
          <Route path="/pools/:id" element={<PoolDetail />} />
          <Route path="/practice" element={<Practice />} />
          <Route path="/learn" element={<Learn />} />
          <Route path="*" element={<Home />} />
        </Routes>
      </main>
      <nav className="tabbar fixed inset-x-0 bottom-0 z-40 border-t border-slate-800 bg-slate-950/95 backdrop-blur">
        <div className="mx-auto flex max-w-xl">
          {TABS.map((t) => (
            <NavLink
              key={t.to}
              to={t.to}
              end={t.to === "/"}
              className={({ isActive }) =>
                `flex flex-1 flex-col items-center py-2 text-[11px] ${isActive ? "text-sky-300" : "text-slate-400"}`
              }
            >
              <span className="text-lg leading-none">{t.icon}</span>
              {t.label}
            </NavLink>
          ))}
        </div>
      </nav>
    </TermProvider>
  );
}
