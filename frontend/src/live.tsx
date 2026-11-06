import { useMemo, useState } from "react";
import { Badge, usePoll } from "./components";
import { Candle, CandleChart, ChartMarker, LinePoint } from "./candles";
import { dayToTs, nyDay } from "./time";
import { Change, liveUrl, quoteFrom } from "./ticker";

const NAMES: Record<string, string> = { SPY: "SPDR S&P 500 ETF · USD", GLD: "SPDR Gold Shares ETF (gold) · USD" };

export const TIMEFRAMES: [string, string][] = [["1Min", "1m"], ["5Min", "5m"], ["15Min", "15m"], ["1Hour", "1H"], ["1Day", "1D"]];

/** Live candlestick panel: timeframe tabs, price header and chart. Decisions/fills/SMA come from Orbit's own records. */
export function LivePanel({ stored: spyStored }: { stored: any }) {
  const [tf, setTf] = useState(() => {
    try { return localStorage.getItem("orbit-tf") || "5Min"; } catch { return "5Min"; }
  });
  const [sym, setSym] = useState(() => {
    try { return localStorage.getItem("orbit-sym") === "GLD" ? "GLD" : "SPY"; } catch { return "SPY"; }
  });
  const pick = (v: string) => { setTf(v); try { localStorage.setItem("orbit-tf", v); } catch { /* per-viewer only */ } };
  const pickSym = (v: string) => { setSym(v); try { localStorage.setItem("orbit-sym", v); } catch { /* per-viewer only */ } };
  const gold = usePoll<any>(sym === "GLD" ? "/api/market/chart?source=auto&symbol=GLD" : null, 120000);
  const stored = sym === "GLD" ? (gold.data?.available ? gold.data : null) : spyStored;
  const daily = usePoll<any>(liveUrl("1Day", sym), 30000);
  const open = !!daily.data?.market?.is_open_by_calendar;
  const live = usePoll<any>(liveUrl(tf, sym), open ? 15000 : 60000);
  const d = live.data?.timeframe === tf && (live.data?.symbol ?? "SPY") === sym ? live.data : null;
  const intraday = tf !== "1Day";
  const q = quoteFrom(daily.data);

  const candles: Candle[] = useMemo(() => {
    if (!d?.available) return [];
    return intraday ? d.candles : d.candles.map((k: Candle) => ({ ...k, t: dayToTs(nyDay(k.t)) }));
  }, [d, intraday]);

  const sma: LinePoint[] = useMemo(() => {
    if (intraday || !stored?.available) return [];
    return stored.bars.filter((b: any) => b.sma200).map((b: any) => ({ t: dayToTs(b.day), value: Number(b.sma200) }));
  }, [stored, intraday]);

  const markers: ChartMarker[] = useMemo(() => {
    if (!stored?.available || stored.dataset?.mode !== "ALPACA") return [];
    const out: ChartMarker[] = [];
    if (!intraday) {
      for (const x of stored.decisions || []) if (x.action === "BUY" || x.action === "SELL") out.push({ t: dayToTs(x.session), side: x.action, kind: "decision", text: `${x.action} signal` });
    }
    const step = candles.length > 1 ? candles[1].t - candles[0].t : 60;
    for (const f of stored.fills || []) {
      const sec = Math.floor(new Date(f.at).getTime() / 1000);
      const t = intraday ? Math.floor(sec / step) * step : dayToTs(nyDay(sec));
      out.push({ t, side: f.side, kind: "fill", text: `${f.side} ${Number(f.qty).toFixed(4)} @ ${Number(f.price).toFixed(2)}` });
    }
    return out;
  }, [stored, intraday, candles]);

  const last = candles[candles.length - 1];
  return (
    <div className="card live-card">
      <div className="live-head">
        <div>
          <div className="seg" role="tablist" aria-label="Fund" style={{ marginBottom: ".4rem" }}>
            {["SPY", "GLD"].map((s) => (
              <button key={s} role="tab" aria-selected={sym === s} className={sym === s ? "on" : ""} onClick={() => pickSym(s)}>{s === "GLD" ? "Gold (GLD)" : "SPY"}</button>
            ))}
          </div>
          <div className="live-sym">{sym} <span className="muted small">{NAMES[sym]}</span></div>
          {q ? (
            <div className="live-price">
              <span className="num">{q.price.toFixed(2)}</span> <Change change={q.change} pct={q.pct} />
            </div>
          ) : <div className="live-price muted">—</div>}
          <div className="small muted">
            {q?.at ? <>Last trade {new Date(q.at).toLocaleTimeString("en-GB", { timeZone: "Asia/Dubai", hour: "2-digit", minute: "2-digit", second: "2-digit" })} Dubai · </> : null}
            {open ? <Badge kind="ok" icon="●">Market open</Badge> : <Badge kind="" icon="☾">Market closed</Badge>}
          </div>
        </div>
        <div className="seg" role="tablist" aria-label="Candle size">
          {TIMEFRAMES.map(([v, label]) => (
            <button key={v} role="tab" aria-selected={tf === v} className={tf === v ? "on" : ""} onClick={() => pick(v)}>{label}</button>
          ))}
        </div>
      </div>
      {live.data && !live.data.available && <div className="banner info"><span aria-hidden="true">ℹ</span><div>{live.data.reason}</div></div>}
      {live.error && <div className="banner bad"><div>{live.error}</div></div>}
      {candles.length > 0 ? (
        <CandleChart candles={candles} sma={sma} markers={markers} intraday={intraday} resetKey={`${sym}:${tf}`}
          ariaLabel={`${sym} ${tf} candlestick chart${last ? `, last close ${last.c.toFixed(2)} USD` : ""}.`} />
      ) : <div className="candle-box skeleton" style={{ height: 420 }} aria-hidden="true" />}
      <div className="legend small muted">
        <span><i className="sw up" /> closed higher than it opened</span>
        <span><i className="sw down" /> closed lower</span>
        {!intraday && <span><i className="sw sma" /> 200-session average (Orbit's records)</span>}
        <span>▲▼ {sym === "GLD" ? "Gold AI buy/sell calls" : "Orbit signals"} · ● fills</span>
      </div>
      <p className="small muted" style={{ marginBottom: 0 }}>
        {d?.note || "Display only."} {intraday && "Intraday candles include pre-market and after-hours trades (thin, can look jumpy). Times in Dubai."}
        {" "}Updates every {open ? "15" : "60"} s.
      </p>
    </div>
  );
}
