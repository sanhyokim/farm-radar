// 線の絵（1つの種類にそろえる。オーナー依頼 10）。24×24、線の太さ 1.75。
const PATHS = {
  home: <><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9v12h14V9" /></>,
  venue: <><path d="M3 21h18" /><path d="M5 21V9l7-5 7 5v12" /><path d="M10 21v-5h4v5" /></>,
  pools: <><path d="M9 6h11" /><path d="M9 12h11" /><path d="M9 18h11" /><path d="M4 6h.01" /><path d="M4 12h.01" /><path d="M4 18h.01" /></>,
  flask: <><path d="M9 3h6" /><path d="M10 3v6l-5.5 10a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3" /><path d="M7.5 15h9" /></>,
  learn: <><path d="M2 4h6a4 4 0 0 1 4 4v13a3 3 0 0 0-3-3H2z" /><path d="M22 4h-6a4 4 0 0 0-4 4v13a3 3 0 0 1 3-3h7z" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  alert: <><path d="M12 3.5 2.5 20h19z" /><path d="M12 10v4.5" /><path d="M12 17.5h.01" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" /></>,
  checkc: <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
  check: <path d="m5 12 5 5 9-10" />,
  down: <path d="m6 9 6 6 6-6" />,
  up: <path d="m6 15 6-6 6 6" />,
  right: <path d="m9 6 6 6-6 6" />,
  left: <path d="m15 6-6 6 6 6" />,
  shield: <path d="M12 3 5 6v6c0 4 3 7.5 7 9 4-1.5 7-5 7-9V6z" />,
  swap: <><path d="M4 8h14l-3-3" /><path d="M20 16H6l3 3" /></>,
  sortDown: <><path d="M12 5v14" /><path d="m6 13 6 6 6-6" /></>,
  sortUp: <><path d="M12 19V5" /><path d="m6 11 6-6 6 6" /></>,
  sort: <><path d="m8 9 4-4 4 4" /><path d="m8 15 4 4 4-4" /></>,
  pause: <><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></>,
  play: <path d="M7 5v14l11-7z" />,
  xc: <><circle cx="12" cy="12" r="9" /><path d="m9 9 6 6" /><path d="m15 9-6 6" /></>,
  x: <><path d="M6 6l12 12" /><path d="M18 6 6 18" /></>,
  flag: <><path d="M5 21V4" /><path d="M5 4h12l-2 4 2 4H5" /></>,
  expand: <><path d="M15 3h6v6" /><path d="M21 3l-7 7" /><path d="M9 21H3v-6" /><path d="M3 21l7-7" /></>,
  filter: <path d="M3 5h18l-7 8v6l-4 2v-8z" />,
  refresh: <><path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" /></>,
  download: <><path d="M12 4v11" /><path d="m7 10 5 5 5-5" /><path d="M5 20h14" /></>,
  list: <><path d="M4 6h16" /><path d="M4 12h16" /><path d="M4 18h10" /></>,
  calendar: <><rect x="3.5" y="5" width="17" height="15" rx="2" /><path d="M3.5 10h17" /><path d="M8 3v4" /><path d="M16 3v4" /></>,
  eye: <><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></>,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></>,
  link: <><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" /><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" /></>,
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 20, color, className = "" }: { name: IconName; size?: number; color?: string; className?: string }) {
  return (
    <svg className={`ico ${className}`} width={size} height={size} viewBox="0 0 24 24" aria-hidden="true"
      style={color ? { color } : undefined}>
      {PATHS[name]}
    </svg>
  );
}
