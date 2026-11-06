import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ErrorNote, Loading, UsdAed, usePoll } from "../components";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function Signed({ v, suffix = "" }: { v: string; suffix?: string }) {
  const n = Number(v);
  return <span className={n > 0 ? "chg up" : n < 0 ? "chg down" : ""}>{n > 0 ? "+" : ""}{v}{suffix}</span>;
}

function PnlChart({ series }: { series: any[] }) {
  const data = series.map((p) => ({ day: p.day, strategy: Number(p.strategy), benchmark: Number(p.benchmark) }));
  return (
    <div className="chart-box" role="img" aria-label="Profit or loss of the rule compared with buying and holding the same money, by day.">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
          <XAxis dataKey="day" tick={{ fill: "var(--muted)", fontSize: 11 }} minTickGap={40} />
          <YAxis tick={{ fill: "var(--muted)", fontSize: 11 }} width={60} tickFormatter={(v) => `$${v}`} />
          <ReferenceLine y={0} stroke="var(--muted)" />
          <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)" }} formatter={(v: any) => `USD ${Number(v).toFixed(2)}`} />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line dataKey="strategy" name="Orbit rule" stroke="var(--accent)" dot={data.length < 30} strokeWidth={2} isAnimationActive={false} />
          <Line dataKey="benchmark" name="Just buy and hold" stroke="var(--chart-sma)" dot={false} strokeDasharray="5 3" strokeWidth={1.6} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function MonthGrid({ months }: { months: any[] }) {
  const years = [...new Set(months.map((m) => m.month.slice(0, 4)))];
  const by = new Map(months.map((m) => [m.month, m]));
  return (
    <div className="table-wrap">
      <table className="month-grid">
        <thead><tr><th>Year</th>{MONTHS.map((m) => <th key={m}>{m}</th>)}</tr></thead>
        <tbody>
          {years.map((y) => (
            <tr key={y}>
              <th scope="row">{y}</th>
              {MONTHS.map((_, i) => {
                const m = by.get(`${y}-${String(i + 1).padStart(2, "0")}`);
                if (!m) return <td key={i} className="muted">·</td>;
                const n = Number(m.strategy);
                return (
                  <td key={i} className={`num ${n > 0 ? "cell-up" : n < 0 ? "cell-down" : ""}`} title={`Rule ${m.strategy} USD, buy-and-hold ${m.benchmark} USD`}>
                    {n > 0 ? "+" : ""}{n.toFixed(2)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Performance({ symbol = "SPY", embedded = false }: { symbol?: string; embedded?: boolean }) {
  const { data: p, error } = usePoll<any>(`/api/performance?symbol=${symbol}`, 120000);
  if (error) return <ErrorNote error={error} />;
  if (!p) return <Loading />;
  const who = symbol === "GLD" ? "the gold AI" : "the rule";
  return (
    <div>
      {embedded ? <h2>{symbol} performance</h2> : <h1>Performance</h1>}
      <p className="muted">Is {who} actually helping? This compares Orbit's real paper trades with simply buying the same money of {symbol} and never selling.</p>
      {!p.available ? (
        <div className="banner info"><span aria-hidden="true">ℹ</span><div>{p.reason}</div></div>
      ) : (
        <>
          <div className={`banner ${p.verdict.code === "BEHIND" ? "warn" : "info"}`}>
            <span aria-hidden="true">{p.verdict.code === "AHEAD" ? "▲" : p.verdict.code === "BEHIND" ? "▼" : "ℹ"}</span>
            <div><strong>{p.verdict.code === "TOO_EARLY" ? "Too early to judge." : p.verdict.code === "AHEAD" ? "Ahead." : p.verdict.code === "BEHIND" ? "Behind." : "Level."}</strong> {p.verdict.text}</div>
          </div>
          <div className="grid g4">
            <div className="card tight">
              <div className="kpi-label">Orbit rule profit / loss</div>
              <div className="kpi-value"><UsdAed usd={p.summary.strategy_pnl} /></div>
              <div className="small"><Signed v={p.summary.strategy_pct} suffix="%" /> <span className="muted">of money used</span></div>
            </div>
            <div className="card tight">
              <div className="kpi-label">If you just held</div>
              <div className="kpi-value"><UsdAed usd={p.summary.benchmark_pnl} /></div>
              <div className="small"><Signed v={p.summary.benchmark_pct} suffix="%" /></div>
            </div>
            <div className="card tight">
              <div className="kpi-label">Difference</div>
              <div className="kpi-value"><UsdAed usd={p.summary.difference} /></div>
              <div className="muted small">Positive means the rule did better.</div>
            </div>
            <div className="card tight">
              <div className="kpi-label">Worst dip from a high</div>
              <div className="kpi-value"><UsdAed usd={p.summary.max_drawdown} /></div>
              <div className="muted small">Biggest fall in the rule's profit from its best point.</div>
            </div>
          </div>

          <div className="card">
            <h2>Profit or loss over time</h2>
            <p className="muted small">{p.sessions} trading day{p.sessions === 1 ? "" : "s"}, up to the close of {p.as_of}. When the two lines split apart, it is because the rule sold and the "just hold" line did not.</p>
            <PnlChart series={p.series} />
          </div>

          <div className="grid g2">
            <div className="card">
              <h2>Month by month</h2>
              <p className="muted small">Profit or loss of the rule each month, in USD. Hover a month to compare with buy-and-hold.</p>
              <MonthGrid months={p.months} />
            </div>
            <div className="card">
              <h2>Trades</h2>
              <table className="small"><tbody>
                <tr><th scope="row">Money used (largest amount invested)</th><td><UsdAed usd={p.summary.money_used} /></td></tr>
                <tr><th scope="row">Locked-in profit (from sales)</th><td><UsdAed usd={p.summary.realized} /></td></tr>
                <tr><th scope="row">Paper profit (still holding)</th><td><UsdAed usd={p.summary.unrealized} /></td></tr>
                <tr><th scope="row">Days invested / in cash</th><td>{p.summary.invested_days} / {p.summary.cash_days}</td></tr>
                <tr><th scope="row">Completed trades (buy then sell)</th><td>{p.summary.round_trips} ({p.summary.wins} won, {p.summary.losses} lost)</td></tr>
              </tbody></table>
              {p.open_trade && <p className="small">Open now: bought from {p.open_trade.opened}, cost <UsdAed usd={p.open_trade.cost} />. It counts as finished when the rule sells.</p>}
              {p.trades.length > 0 && (
                <div className="table-wrap"><table className="small">
                  <thead><tr><th>Bought</th><th>Sold</th><th>Cost</th><th>Result</th></tr></thead>
                  <tbody>{p.trades.map((t: any, i: number) => <tr key={i}><td>{t.opened}</td><td>{t.closed}</td><td><UsdAed usd={t.cost} /></td><td><Signed v={t.pnl} /> (<Signed v={t.pct} suffix="%" />)</td></tr>)}</tbody>
                </table></div>
              )}
            </div>
          </div>
          <ul className="small muted">{p.notes.map((n: string) => <li key={n}>{n}</li>)}<li>AED amounts use the reference rate {p.fx.rate} ({p.fx.source}).</li></ul>
        </>
      )}
    </div>
  );
}
