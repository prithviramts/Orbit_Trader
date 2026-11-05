import { useEffect, useRef } from "react";
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, HistogramSeries, IChartApi, ISeriesApi,
  ISeriesMarkersPluginApi, LineSeries, SeriesMarker, Time, UTCTimestamp,
} from "lightweight-charts";

export type Candle = { t: number; o: number; h: number; l: number; c: number; v?: number };
export type LinePoint = { t: number; value: number };
export type ChartMarker = { t: number; side: "BUY" | "SELL"; text: string; kind: "decision" | "fill" };

function cssVar(name: string, fallback: string) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function theme() {
  return {
    bg: cssVar("--panel", "#11151c"),
    text: cssVar("--muted", "#8b93a7"),
    grid: cssVar("--grid", "#1b212c"),
    line: cssVar("--line", "#232a36"),
    up: cssVar("--up", "#26a69a"),
    down: cssVar("--down", "#ef5350"),
    sma: cssVar("--chart-sma", "#f5b84b"),
    accent: cssVar("--accent", "#4c8dff"),
  };
}

/**
 * Canvas candlestick chart (TradingView Lightweight Charts, Apache-2.0). Green = closed higher than it opened,
 * red = closed lower. Volume is drawn faintly underneath. Updates in place, so polling does not reset zoom.
 */
export function CandleChart({
  candles, sma, markers = [], intraday, height = 420, ariaLabel, resetKey,
}: {
  candles: Candle[]; sma?: LinePoint[]; markers?: ChartMarker[]; intraday: boolean; height?: number; ariaLabel: string; resetKey: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const cs = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vol = useRef<ISeriesApi<"Histogram"> | null>(null);
  const line = useRef<ISeriesApi<"Line"> | null>(null);
  const mk = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const fitted = useRef<string>("");

  // create once per timeframe/theme
  useEffect(() => {
    if (!box.current) return;
    const t = theme();
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: t.bg }, textColor: t.text, fontSize: 11, attributionLogo: true,
        fontFamily: getComputedStyle(document.body).fontFamily },
      grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
      rightPriceScale: { borderColor: t.line },
      timeScale: {
        borderColor: t.line, timeVisible: intraday, secondsVisible: false, rightOffset: 4,
        tickMarkFormatter: (time: Time, type: number) => tick(time as number, type, intraday),
      },
      crosshair: { mode: CrosshairMode.Normal },
      localization: {
        timeFormatter: (time: Time) => fmtTime(time as number, intraday),
        priceFormatter: (p: number) => p.toFixed(2),
      },
    });
    cs.current = c.addSeries(CandlestickSeries, {
      upColor: t.up, downColor: t.down, borderUpColor: t.up, borderDownColor: t.down, wickUpColor: t.up, wickDownColor: t.down,
      priceLineVisible: true,
    });
    vol.current = c.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    line.current = c.addSeries(LineSeries, { color: t.sma, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    mk.current = createSeriesMarkers(cs.current, []);
    chart.current = c;
    fitted.current = "";
    return () => {
      c.remove();
      chart.current = null;
    };
  }, [intraday, resetKey]);

  // push data
  useEffect(() => {
    if (!cs.current || !vol.current || !line.current || !mk.current) return;
    const t = theme();
    cs.current.setData(candles.map((k) => ({ time: k.t as UTCTimestamp, open: k.o, high: k.h, low: k.l, close: k.c })));
    vol.current.setData(candles.map((k) => ({
      time: k.t as UTCTimestamp, value: k.v ?? 0,
      color: (k.c >= k.o ? t.up : t.down) + "55",
    })));
    line.current.setData((sma || []).map((p) => ({ time: p.t as UTCTimestamp, value: p.value })));
    const ms: SeriesMarker<Time>[] = [...markers].sort((a, b) => a.t - b.t).map((m) => ({
      time: m.t as UTCTimestamp,
      position: m.side === "BUY" ? "belowBar" : "aboveBar",
      shape: m.kind === "fill" ? "circle" : m.side === "BUY" ? "arrowUp" : "arrowDown",
      color: m.side === "BUY" ? t.up : t.down,
      text: m.text,
    }));
    mk.current.setMarkers(ms);
    if (fitted.current !== resetKey && candles.length) {
      const ts = chart.current?.timeScale();
      const n = candles.length;
      const show = intraday ? 120 : 260;
      if (ts) ts.setVisibleLogicalRange({ from: Math.max(0, n - show), to: n + 3 });
      fitted.current = resetKey;
    }
  }, [candles, sma, markers, intraday, resetKey]);

  return <div ref={box} className="candle-box" style={{ height }} role="img" aria-label={ariaLabel} />;
}

function tick(sec: number, type: number, intraday: boolean) {
  const d = new Date(sec * 1000);
  if (!intraday) {
    if (type === 0) return d.toLocaleDateString("en-GB", { timeZone: "UTC", year: "numeric" });
    if (type === 1) return d.toLocaleDateString("en-GB", { timeZone: "UTC", month: "short" });
    return d.toLocaleDateString("en-GB", { timeZone: "UTC", day: "2-digit", month: "short" });
  }
  if (type < 3) return d.toLocaleDateString("en-GB", { timeZone: "Asia/Dubai", day: "2-digit", month: "short" });
  return d.toLocaleTimeString("en-GB", { timeZone: "Asia/Dubai", hour: "2-digit", minute: "2-digit" });
}

export function fmtTime(sec: number, intraday: boolean) {
  const d = new Date(sec * 1000);
  return intraday
    ? d.toLocaleString("en-GB", { timeZone: "Asia/Dubai", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) + " Dubai"
    : d.toLocaleDateString("en-GB", { timeZone: "UTC", day: "2-digit", month: "short", year: "numeric" });
}

/** "2026-11-04" -> unix seconds at 00:00 UTC (daily bars are keyed by session date). */
export { dayToTs, nyDay } from "./time";
