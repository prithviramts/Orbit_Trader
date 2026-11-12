import { useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Badge, ErrorNote, Loading, UsdAed, usePoll } from "../components";

const STYLE: Record<string, { color: string; dash?: boolean }> = {
  spy_rule: { color: "var(--accent)" },
  spy_hold: { color: "var(--muted)", dash: true },
  spy_risk: { color: "var(--chart-b2)" },
  gold_hold: { color: "var(--chart-b1)", dash: true },
  gold_rule: { color: "var(--cmp-orange)" },
  mix_70_30: { color: "var(--cmp-teal)" },
};

function Pct({ v }: { v: number | string | null | undefined }) {
  if (v === null || v === undefined) return <span className="muted">—</span>;
  const n = Number(v);
  return <span className={n > 0 ? "chg up" : n < 0 ? "chg down" : ""}>{n > 0 ? "+" : ""}{n.toFixed(2)}%</span>;
}

function holdingText(h: Record<string, number> | undefined) {
  if (!h) return "—";
  const parts = Object.entries(h).filter(([, w]) => w > 0).map(([k, w]) => `${Math.round(w * 100)}% ${k === "GLD" ? "gold" : k}`);
  return parts.length ? parts.join(" + ") : "Cash";
}

function Key({ id }: { id: string }) {
  const s = STYLE[id] ?? { color: "var(--muted)" };
  return <span className={`cmp-key${s.dash ? " dash" : ""}`} style={{ background: s.color, color: s.color }} aria-hidden="true" />;
}

/** Every strategy side by side: real paper results, then the shadow lab (virtual USD 1,000 each, no orders). */
export function Compare() {
  const { data, error } = usePoll<any>("/api/compare", 300000);
  const [win, setWin] = useState<"live" | "history">("live");
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading />;
  const lab = data.lab;
  const ideas: any[] = lab?.available ? lab.ideas : [];
  const rows = ideas.map((i) => ({ ...i, w: i[win] })).filter((i) => i.w?.available);
  const chart: Record<string, any> = {};
  for (const i of rows) for (const p of i.w.series) (chart[p.day] ??= { day: p.day })[i.id] = p.value;
  const chartData = Object.values(chart).sort((a: any, b: any) => (a.day < b.day ? -1 : 1));

  return (
    <>
      <div className="card">
        <h2>Real paper results</h2>
        <p className="muted small">From Orbit's actual paper trades, compared with buying the same money and never selling.</p>
        <div className="table-wrap"><table className="small">
          <thead><tr><th>Strategy</th><th>Profit / loss</th><th>On money used</th><th>If just held</th><th>Verdict</th></tr></thead>
          <tbody>
            {data.real.map((r: any) => (
              <tr key={r.symbol}>
                <th scope="row">{r.name}</th>
                {r.available ? (
                  <>
                    <td><UsdAed usd={r.pnl} /></td>
                    <td><Pct v={r.pct} /></td>
                    <td><UsdAed usd={r.benchmark_pnl} /> (<Pct v={r.benchmark_pct} />)</td>
                    <td>{r.verdict.code === "TOO_EARLY" ? <Badge kind="">Too early</Badge> : r.verdict.code}</td>
                  </>
                ) : <td colSpan={4} className="muted">{r.reason}</td>}
              </tr>
            ))}
          </tbody>
        </table></div>
      </div>

      <div className="card">
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: ".5rem" }}>
          <h2 style={{ margin: 0 }}>Shadow lab: ideas side by side</h2>
          <div className="seg" role="tablist" aria-label="Period">
            <button role="tab" aria-selected={win === "live"} className={win === "live" ? "on" : ""} onClick={() => setWin("live")}>Since Orbit started</button>
            <button role="tab" aria-selected={win === "history"} className={win === "history" ? "on" : ""} onClick={() => setWin("history")}>2022 to now</button>
          </div>
        </div>
        {!lab?.available ? (
          <div className="banner info" style={{ marginTop: ".6rem" }}><span aria-hidden="true">ℹ</span><div>{lab?.reason ?? "Not computed yet."}{lab?.error ? ` Last try: ${lab.error}` : ""}</div></div>
        ) : (
          <>
            <p className="muted small">
              Each line is a virtual USD 1,000 that decides on the close and trades at the next open. <strong>Shadow ideas never place orders.</strong>{" "}
              {win === "live" ? `Since ${lab.start_live}, up to the close of ${lab.as_of}.` : `From ${lab.history_start} to ${lab.as_of}: context only, past results do not promise future ones.`}
            </p>
            <div className="chart-box" role="img" aria-label="Value of a virtual USD 1,000 in each strategy over time.">
              <ResponsiveContainer>
                <LineChart data={chartData} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
                  <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
                  <XAxis dataKey="day" tick={{ fill: "var(--muted)", fontSize: 11 }} minTickGap={40} />
                  <YAxis tick={{ fill: "var(--muted)", fontSize: 11 }} width={60} domain={["auto", "auto"]} tickFormatter={(v) => `$${Math.round(v)}`} />
                  <ReferenceLine y={1000} stroke="var(--muted)" />
                  <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)" }}
                    formatter={(v: any, name: any) => [`USD ${Number(v).toFixed(2)}`, rows.find((r) => r.id === name)?.name ?? name]} />
                  {rows.map((i) => (
                    <Line key={i.id} dataKey={i.id} name={i.id} stroke={STYLE[i.id]?.color} strokeDasharray={STYLE[i.id]?.dash ? "5 3" : undefined}
                      dot={chartData.length < 30} strokeWidth={i.id === "spy_rule" ? 2.4 : 1.6} isAnimationActive={false} />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="table-wrap"><table className="small">
              <thead><tr><th>Idea</th><th>USD 1,000 is now</th><th>Return</th><th>Worst fall</th><th>Trades</th><th>Holding now</th></tr></thead>
              <tbody>
                {rows.map((i) => (
                  <tr key={i.id} className={i.id === "spy_rule" ? "cmp-real" : ""}>
                    <th scope="row" title={i.about}>
                      <Key id={i.id} />{i.name}{" "}
                      {i.kind === "reference" ? <Badge kind="">{i.id === "spy_rule" ? "Orbit's rule" : "Yardstick"}</Badge> : <Badge kind="info">Shadow</Badge>}
                      <div className="muted" style={{ fontWeight: 400 }}>{i.about}</div>
                    </th>
                    <td><UsdAed usd={i.w.value} /></td>
                    <td><Pct v={i.w.return_pct} /></td>
                    <td><Pct v={-i.w.max_drawdown_pct} /></td>
                    <td className="num">{i.w.trades}</td>
                    <td>{holdingText(i.w.holding)}{i.w.changes_next_open && <div className="small">Next open: {holdingText(i.w.next)}</div>}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
            <ul className="small muted">{lab.notes.map((n: string) => <li key={n}>{n}</li>)}<li>The Gold AI is not re-simulated here: its model retrains, so a replay would look better in hindsight than it really was. Its real result is in the table above.</li></ul>
          </>
        )}
      </div>
    </>
  );
}
