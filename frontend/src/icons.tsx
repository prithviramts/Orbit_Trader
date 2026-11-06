/** Small line-icon set drawn for Orbit (24px grid, 1.8px stroke, inherits text colour). */
const PATHS = {
  home: "M3 11.5 12 4l9 7.5M5.5 9.5V20h13V9.5M10 20v-5h4v5",
  candles: "M7 3v4M7 15v6M5 7h4v8H5zM17 3v6M17 17v4M15 9h4v8h-4z",
  swap: "M4 8h14l-3.5-3.5M20 16H6l3.5 3.5",
  news: "M4 5h13v14a1 1 0 0 0 1 1H6a2 2 0 0 1-2-2zM17 9h3v10a1 1 0 0 1-2 0M7.5 9h6M7.5 12.5h6M7.5 16h4",
  list: "M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01",
  flask: "M9 3h6M10 3v6L4.5 18.5A1.6 1.6 0 0 0 6 21h12a1.6 1.6 0 0 0 1.5-2.5L14 9V3M7.5 14h9",
  book: "M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5zM4 20.5A2.5 2.5 0 0 0 6.5 23H20",
  pen: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  phone: "M7 2.5h10a1 1 0 0 1 1 1v17a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-17a1 1 0 0 1 1-1zM11 18.5h2",
  plug: "M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5",
  gear: "M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4zM19.4 13.5l1.6 1.2-2 3.4-1.9-.7a7.5 7.5 0 0 1-2.1 1.2l-.3 2h-3.9l-.3-2a7.5 7.5 0 0 1-2.1-1.2l-1.9.7-2-3.4 1.6-1.2a7.6 7.6 0 0 1 0-2.9L2.6 9.3l2-3.4 1.9.7a7.5 7.5 0 0 1 2.1-1.2l.3-2h3.9l.3 2a7.5 7.5 0 0 1 2.1 1.2l1.9-.7 2 3.4-1.6 1.2a7.6 7.6 0 0 1 0 2.9z",
  shield: "M12 2.5 4.5 5.5v6c0 4.6 3.2 8.4 7.5 10 4.3-1.6 7.5-5.4 7.5-10v-6zM9 12l2 2 4-4",
  sun: "M12 16.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM12 1.5v2M12 20.5v2M4.6 4.6 6 6M18 18l1.4 1.4M1.5 12h2M20.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4",
  moon: "M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11z",
  logout: "M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 16l-4-4 4-4M6 12h10",
  menu: "M4 7h16M4 12h16M4 17h16",
  bell: "M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2H4.5zM10 21h4",
  trend: "M3 17l6-6 4 4 8-8M15 7h6v6",
  close: "M6 6l12 12M18 6 6 18",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}
