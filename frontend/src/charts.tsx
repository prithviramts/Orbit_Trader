import { CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis, Area, AreaChart, LineChart } from "recharts";

export type Bar = { day: string; close: string | null; sma200: string | null };
export type Marker = { day: string; kind: "BUY" | "SELL"; ref: string | number; label: string };

/** Price + 200-session average. Missing sessions are inserted as nulls so lines show a gap instead of bridging it. */
export function PriceChart({
  bars, missing = [], signals = [], fills = [], onSelect,
}: { bars: Bar[]; missing?: string[]; signals?: Marker[]; fills?: Marker[]; onSelect?: (m: Marker) => void }) {
  const byDay = new Map<string, any>();
  for (const b of bars) byDay.set(b.day, { day: b.day, close: b.close ? Number(b.close) : null, sma: b.sma200 ? Number(b.sma200) : null });
  for (const m of missing) if (!byDay.has(m)) byDay.set(m, { day: m, close: null, sma: null, gap: true });
  for (const s of signals) {
    const p = byDay.get(s.day);
    if (p) { p[s.kind === "BUY" ? "sigBuy" : "sigSell"] = p.close; p.sigRef = s; }
  }
  for (const f of fills) {
    const p = byDay.get(f.day);
    if (p) { p[f.kind === "BUY" ? "fillBuy" : "fillSell"] = p.close; p.fillRef = f; }
  }
  const data = [...byDay.values()].sort((a, b) => (a.day < b.day ? -1 : 1));
  const click = (e: any) => {
    const p = e?.payload;
    const m = p?.sigRef || p?.fillRef;
    if (m && onSelect) onSelect(m);
  };
  return (
    <div className="chart-box" role="img" aria-label="SPY close and 200-session moving average with decision and fill markers. A table alternative follows.">
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
          <XAxis dataKey="day" tick={{ fill: "var(--muted)", fontSize: 11 }} minTickGap={40} />
          <YAxis domain={["auto", "auto"]} tick={{ fill: "var(--muted)", fontSize: 11 }} width={60} />
          <Tooltip content={<PriceTip />} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line dataKey="close" name="Close (USD)" stroke="var(--chart-close)" dot={false} strokeWidth={1.4} isAnimationActive={false} connectNulls={false} />
          <Line dataKey="sma" name="200-session average" stroke="var(--chart-sma)" dot={false} strokeWidth={1.6} strokeDasharray="5 3" isAnimationActive={false} connectNulls={false} />
          <Scatter dataKey="sigBuy" name="Signal: buy (decision)" fill="var(--ok)" shape="triangle" onClick={click} cursor="pointer" isAnimationActive={false} />
          <Scatter dataKey="sigSell" name="Signal: sell (decision)" fill="var(--warn)" shape="diamond" onClick={click} cursor="pointer" isAnimationActive={false} />
          <Scatter dataKey="fillBuy" name="Actual/sim fill: buy" fill="var(--accent)" shape="star" onClick={click} cursor="pointer" isAnimationActive={false} />
          <Scatter dataKey="fillSell" name="Actual/sim fill: sell" fill="var(--bad)" shape="cross" onClick={click} cursor="pointer" isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function PriceTip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="tooltip-card">
      <div><strong>{p.day}</strong>{p.gap && " — missing session (gap preserved)"}</div>
      {p.close != null && <div>Close: {p.close.toFixed(2)} USD</div>}
      {p.sma != null && <div>200-session avg: {p.sma.toFixed(2)} USD</div>}
      {p.sigRef && <div>Decision: {p.sigRef.label} (click to open)</div>}
      {p.fillRef && <div>Fill: {p.fillRef.label} (click to open)</div>}
    </div>
  );
}

export function IndexedChart({ series }: { series: { name: string; color: string; points: [string, string][] }[] }) {
  const base = series.map((s) => Number(s.points[0]?.[1] || 1));
  const rows = series[0]?.points.map((p, i) => {
    const r: any = { day: p[0] };
    series.forEach((s, k) => (r[s.name] = (Number(s.points[i]?.[1]) / base[k]) * 100));
    return r;
  }) || [];
  return (
    <div className="chart-box" role="img" aria-label="Portfolio value indexed to 100 versus both benchmarks.">
      <ResponsiveContainer>
        <LineChart data={rows} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
          <XAxis dataKey="day" tick={{ fill: "var(--muted)", fontSize: 11 }} minTickGap={40} />
          <YAxis domain={["auto", "auto"]} tick={{ fill: "var(--muted)", fontSize: 11 }} width={50} />
          <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)" }} formatter={(v: any) => Number(v).toFixed(2)} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {series.map((s) => <Line key={s.name} dataKey={s.name} stroke={s.color} dot={false} isAnimationActive={false} strokeWidth={1.5} />)}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function DrawdownChart({ points }: { points: [string, string][] }) {
  const rows = points.map(([d, v]) => ({ day: d, dd: Number(v) * 100 }));
  return (
    <div className="chart-box short" role="img" aria-label="Strategy drawdown from its high-water mark, in percent.">
      <ResponsiveContainer>
        <AreaChart data={rows} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
          <XAxis dataKey="day" tick={{ fill: "var(--muted)", fontSize: 11 }} minTickGap={40} />
          <YAxis tick={{ fill: "var(--muted)", fontSize: 11 }} width={50} unit="%" />
          <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)" }} formatter={(v: any) => `${Number(v).toFixed(2)}%`} />
          <Area dataKey="dd" name="Drawdown" stroke="var(--bad)" fill="var(--bad)" fillOpacity={0.2} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
