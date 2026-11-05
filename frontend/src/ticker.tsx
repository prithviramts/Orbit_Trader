import { useMemo } from "react";
import { usePoll } from "./components";
import { nyDay } from "./time";

type Candle = { t: number; c: number };

/** Latest SPY price and change vs the previous session's close, from the daily feed. */
export function useSpyQuote() {
  const { data } = usePoll<any>("/api/market/live?tf=1Day", 30000);
  return useMemo(() => quoteFrom(data), [data]);
}

export function quoteFrom(d: any) {
  if (!d?.available || !d.candles?.length) return null;
  const today = nyDay(Date.now() / 1000);
  const c = d.candles as Candle[];
  const last = c[c.length - 1];
  const prev = nyDay(last.t) === today ? c[c.length - 2] : last;
  const price = d.latest_trade?.price ?? last.c;
  if (!prev) return null;
  const change = price - prev.c;
  return { price, change, pct: change / prev.c, at: d.latest_trade?.at as string | undefined, open: !!d.market?.is_open_by_calendar };
}

export function Change({ change, pct }: { change: number; pct: number }) {
  const up = change >= 0;
  return (
    <span className={`chg ${up ? "up" : "down"}`}>
      <span aria-hidden="true">{up ? "▲" : "▼"}</span> {up ? "+" : ""}{change.toFixed(2)} ({up ? "+" : ""}{(pct * 100).toFixed(2)}%)
      <span className="sr-only">{up ? " up" : " down"} today</span>
    </span>
  );
}

export function TickerChip() {
  const q = useSpyQuote();
  if (!q) return null;
  return (
    <span className="ticker" title="SPY latest trade (IEX feed) vs previous close. Display only.">
      <strong>SPY</strong> <span className="num">{q.price.toFixed(2)}</span> <Change change={q.change} pct={q.pct} />
    </span>
  );
}

