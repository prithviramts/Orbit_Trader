import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { post } from "../api";
import { Badge, DubaiTime, ErrorNote, Loading, Money, usePoll } from "../components";
import { Performance } from "./Performance";

const pctx = (v: number | null | undefined, dp = 0) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(dp)}%`);

function fmtSignal(key: string, v: number) {
  if (key.startsWith("rule_")) return v >= 0.5 ? "yes" : "no";
  if (key === "rsi14") return v.toFixed(0);
  if (key === "boll") return `${v.toFixed(2)} sd`;
  if (key === "corr60") return v.toFixed(2);
  if (key === "up_days") return `${(v * 100).toFixed(0)}%`;
  return `${(v * 100).toFixed(2)}%`;
}

function Gauge({ p, enter, exit }: { p: number; enter: number; exit: number }) {
  return (
    <div className="gauge" role="img" aria-label={`Chance ${pctx(p)}; buy above ${pctx(enter)}, sell below ${pctx(exit)}`}>
      <div className="gauge-zone sell" style={{ width: `${exit * 100}%` }} />
      <div className="gauge-zone hold" style={{ left: `${exit * 100}%`, width: `${(enter - exit) * 100}%` }} />
      <div className="gauge-zone buy" style={{ left: `${enter * 100}%`, width: `${(1 - enter) * 100}%` }} />
      <div className="gauge-needle" style={{ left: `${p * 100}%` }} />
      <div className="gauge-labels small muted"><span>0%</span><span>sell &lt; {pctx(exit)}</span><span>buy &gt; {pctx(enter)}</span><span>100%</span></div>
    </div>
  );
}

function Switch({ g, reload }: { g: any; reload: () => void }) {
  const [budget, setBudget] = useState(g.settings.budget_usd);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setBudget(g.settings.budget_usd), [g.settings.budget_usd]);
  const save = async (enabled: boolean) => {
    setErr(null);
    setBusy(true);
    try {
      await post("/api/gold/settings", { enabled, budget_usd: budget });
      reload();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const on = g.settings.enabled;
  return (
    <div className="card">
      <h2>AI trading switch</h2>
      <p>
        {on ? <Badge kind="ok" icon="●">Trading with paper money</Badge> : <Badge kind="" icon="○">Shadow mode: predicting only</Badge>}{" "}
        {g.mode !== "PAPER_AUTO" && <Badge kind="warn" icon="!">Orbit is in OBSERVE mode, so no orders</Badge>}
      </p>
      <div className="field">
        <label htmlFor="gold-budget">Money per gold buy (USD, max {g.cap_usd})</label>
        <input id="gold-budget" type="number" min={1} max={Number(g.cap_usd)} step="1" value={budget} onChange={(e) => setBudget(e.target.value)} style={{ width: "8rem" }} />
      </div>
      <div className="row">
        {on ? (
          <>
            <button onClick={() => save(true)} disabled={busy}>Save budget</button>
            <button className="danger" onClick={() => save(false)} disabled={busy}>Stop AI trading (keep predicting)</button>
          </>
        ) : (
          <button className="primary" onClick={() => save(true)} disabled={busy}>Let the AI trade gold (paper)</button>
        )}
      </div>
      <ErrorNote error={err} />
      <p className="small muted">
        It buys at the next US open when its chance is above the buy line and sells when it drops below the sell line.
        Orbit's pauses, reviews and safety checks apply to gold too. Stopping does not sell what it already holds;
        the next SELL call does.
      </p>
    </div>
  );
}

export function Gold() {
  const { data: g, error, reload } = usePoll<any>("/api/gold", 60000);
  if (error) return <ErrorNote error={error} />;
  if (!g) return <Loading />;
  const m = g.model;
  const L = g.latest;
  const ev = m?.evaluation;
  const hist = (g.history ?? []).filter((h: any) => h.probability !== null && h.probability !== undefined)
    .map((h: any) => ({ day: h.session, p: Math.round(h.probability * 1000) / 10 }));
  return (
    <div>
      <h1>Gold AI</h1>
      <p className="muted">A learning model that reads 26 signals about gold (GLD) and stocks (SPY) every evening and estimates the chance gold is higher 20 trading days later. Paper money only.</p>
      <div className="banner warn"><span aria-hidden="true">!</span><div><strong>Live experiment.</strong> {g.honesty.join(" ")}</div></div>

      <div className="grid g2">
        <div className="card">
          <h2>Today's call</h2>
          {!L ? (
            <p className="muted">No gold decision yet. The AI decides after each US close (about 01:15 Dubai), once its model is trained.</p>
          ) : L.probability === null || L.probability === undefined ? (
            <p>{L.reason}</p>
          ) : (
            <>
              <div className="kpi-value">{pctx(L.probability)} <span className="muted small">chance GLD is higher in 20 trading days</span></div>
              <Gauge p={L.probability} enter={m?.enter ?? 0.55} exit={m?.exit ?? 0.45} />
              <p>
                <Badge kind={L.action === "BUY" ? "ok" : L.action === "SELL" ? "bad" : ""}>{L.action}</Badge>{" "}
                {L.would_action && <span className="muted">would {L.would_action}</span>}{" "}
                <span className="muted small">session {L.session} · GLD close USD {L.close}</span>
              </p>
              <p className="small">{L.reason}</p>
            </>
          )}
        </div>
        <Switch g={g} reload={reload} />
      </div>

      {L?.signals?.length > 0 && (
        <div className="card">
          <h2>Why: the signals behind today's chance</h2>
          <p className="muted small">Each bar is how hard that signal pushed today's chance up (green) or down (red), after the model's learned weighting.</p>
          <div className="table-wrap"><table className="small">
            <thead><tr><th>Signal</th><th>Today</th><th>Push</th></tr></thead>
            <tbody>{L.signals.map((x: any) => {
              const w = Math.min(100, Math.abs(x.push) * 120);
              return (
                <tr key={x.key}>
                  <td>{x.label}</td>
                  <td className="num">{fmtSignal(x.key, x.value)}</td>
                  <td style={{ minWidth: 140 }}><div className={`push ${x.push >= 0 ? "pos" : "neg"}`} style={{ width: `${w}%` }} title={x.push.toFixed(3)} /></td>
                </tr>
              );
            })}</tbody>
          </table></div>
        </div>
      )}

      <div className="grid g2">
        <div className="card">
          <h2>The model</h2>
          {!m ? (
            <p className="muted">Not trained yet. The worker trains it automatically from Alpaca's daily history since 2016 (it takes a few seconds), then retrains every 28 days. {g.training?.error && <span className="chg down">Last attempt failed: {g.training.error}</span>}</p>
          ) : (
            <>
              <table className="small"><tbody>
                <tr><th scope="row">Version</th><td>{m.version}</td></tr>
                <tr><th scope="row">Learned from</th><td>{m.rows.toLocaleString()} trading days, up to {m.trained_through}</td></tr>
                <tr><th scope="row">Trained</th><td>{m.trained_at?.replace("T", " ").replace("Z", " UTC")}</td></tr>
                <tr><th scope="row">Rule</th><td>buy above {pctx(m.enter)}, sell below {pctx(m.exit)}, look-ahead {m.horizon_days} trading days</td></tr>
              </tbody></table>
              {ev?.model && (
                <>
                  <h3>Test on years it had not seen ({ev.from?.slice(0, 4)}–{ev.to?.slice(0, 4)})</h3>
                  <table className="small"><thead><tr><th></th><th>Return</th><th>Worst fall</th></tr></thead><tbody>
                    <tr><th scope="row">Gold AI</th><td>{ev.model.total_return_pct}%</td><td>−{ev.model.max_drawdown_pct}%</td></tr>
                    <tr><th scope="row">Just hold gold</th><td>{ev.hold.total_return_pct}%</td><td>−{ev.hold.max_drawdown_pct}%</td></tr>
                  </tbody></table>
                  <p className="small">Direction calls right: <strong>{ev.direction_hit_rate_pct}%</strong> (saying "up" every time: {ev.always_up_rate_pct}%). Trades: {ev.fills}. Time invested: {ev.time_invested_pct}%.</p>
                </>
              )}
            </>
          )}
        </div>
        <div className="card">
          <h2>What it learned to trust</h2>
          {!m ? <p className="muted">Appears after training.</p> : (
            <>
              <p className="muted small">The weights the model gives each signal (after scaling). Positive leans towards buying when the signal is high.</p>
              <div className="table-wrap"><table className="small"><tbody>
                {m.weights.slice(0, 12).map((w: any) => (
                  <tr key={w.key}><td>{w.label}</td><td className={`num ${w.weight >= 0 ? "chg up" : "chg down"}`}>{w.weight >= 0 ? "+" : ""}{w.weight.toFixed(3)}</td></tr>
                ))}
              </tbody></table></div>
            </>
          )}
        </div>
      </div>

      {hist.length > 1 && (
        <div className="card">
          <h2>Its chance, day by day</h2>
          <div className="chart-box">
            <ResponsiveContainer>
              <LineChart data={hist} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="var(--line)" strokeDasharray="3 3" />
                <XAxis dataKey="day" tick={{ fill: "var(--muted)", fontSize: 11 }} minTickGap={40} />
                <YAxis domain={[0, 100]} tick={{ fill: "var(--muted)", fontSize: 11 }} width={40} tickFormatter={(v) => `${v}%`} />
                <ReferenceLine y={(m?.enter ?? 0.55) * 100} stroke="var(--up)" strokeDasharray="4 3" />
                <ReferenceLine y={(m?.exit ?? 0.45) * 100} stroke="var(--down)" strokeDasharray="4 3" />
                <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)" }} formatter={(v: any) => `${v}%`} />
                <Line dataKey="p" name="Chance up" stroke="var(--accent)" dot={hist.length < 30} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      <div className="grid g2">
        <div className="card">
          <h2>Live scorecard</h2>
          <p className="muted small">{g.scorecard.meaning}</p>
          {g.scorecard.scored === 0 ? (
            <p className="muted">No calls can be scored yet: each one needs 20 trading days (about 4 weeks) to play out.</p>
          ) : (
            <p>Right <strong>{g.scorecard.hit_rate_pct}%</strong> of {g.scorecard.scored} calls · "always up" would be {g.scorecard.always_up_pct}%</p>
          )}
        </div>
        <div className="card">
          <h2>Gold holding</h2>
          {g.position && Number(g.position.held_qty) > 0 ? (
            <p>Holding <strong>{Number(g.position.held_qty).toFixed(4)} GLD</strong>, cost <Money v={g.position.cost_basis} /></p>
          ) : <p className="muted">No gold held right now.</p>}
          {g.open_intents.length > 0 && <p className="small">Order waiting: {g.open_intents.map((i: any) => `${i.side} for ${i.execution_session} (${i.state})`).join(", ")}</p>}
          {g.training?.last_attempt && <p className="small muted">Last training attempt: <DubaiTime t={{ utc: g.training.last_attempt, dubai: "", new_york: "" }} /></p>}
        </div>
      </div>

      <Performance symbol="GLD" embedded />
    </div>
  );
}
