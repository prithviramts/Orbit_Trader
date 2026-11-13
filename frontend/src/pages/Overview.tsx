import { Link } from "react-router-dom";
import { LivePanel } from "../live";
import { useQuote } from "../ticker";
import { Badge, DubaiTime, ErrorNote, Loading, Money, pct, StateBadge, usePoll } from "../components";

export function Overview() {
  const { data: o, error } = usePoll<any>("/api/overview", 15000);
  const ev = usePoll<any>("/api/events", 30000);
  const chart = usePoll<any>("/api/market/chart?source=auto", 120000);
  if (error) return <ErrorNote error={error} />;
  if (!o) return <Loading />;
  const d = o.last_decision;
  const nextExec = o.open_intents.find((i: any) => i.state === "PENDING");
  return (
    <div>
      <h1>Overview</h1>
      <section className="card" aria-label="Paper execution status">
        <div className="row">
          <Badge kind="accent">Paper</Badge>
          <Badge kind="">{o.mode === "PAPER_AUTO" ? "Automation enabled" : "Observe only"}</Badge>
          <Badge kind="">Worker: {o.worker.state.replaceAll("_", " ")}</Badge>
          {o.flags.new_orders_paused && <Badge kind="warn">New orders paused</Badge>}
          {o.flags.risk_paused_entries && <Badge kind="warn">Entries paused</Badge>}
          {o.flags.security_paused && <Badge kind="bad">Security pause</Badge>}
        </div>
        <p className="small">
          Latest reconciliation: {o.reconciliation ? <>{o.reconciliation.ok ? "Clean" : "Discrepancies"} · <DubaiTime t={o.reconciliation.at} /></> : "Unavailable"}
          {" · "}Latest decision data: {d?.data_fetched_at ? <DubaiTime t={d.data_fetched_at} /> : "Unavailable"}
        </p>
        <h2>Open order intents</h2>
        {o.open_intents.length ? (
          <ul>{o.open_intents.map((i: any) => (
            <li key={i.id}>
              <strong>{i.side} {i.symbol}</strong> <StateBadge state={i.state} />
              {i.state === "PENDING" && <> · Planned for {i.execution_session}</>}
              {i.reason && <> · {i.reason}</>}
            </li>
          ))}</ul>
        ) : <p className="muted">No open order intents.</p>}
        <Link to="/orders">Review orders and controls →</Link>
      </section>

      {o.incidents.length > 0 && (
        <section aria-label="Unresolved incidents">
          {o.incidents.map((x: any, i: number) => (
            <div key={i} className={`banner ${x.severity === "critical" ? "bad" : "warn"}`} role="alert">
              <span aria-hidden="true">{x.severity === "critical" ? "⛔" : "!"}</span>
              <div>
                <strong>{x.kind.replaceAll("_", " ")}</strong> — {x.text}{" "}
                {x.intent_id && <Link to="/orders">Review on Orders</Link>}
                {x.kind === "SECURITY_PAUSED" && <Link to="/security"> Incident checklist</Link>}
              </div>
            </div>
          ))}
        </section>
      )}
      {!o.broker_configured && (
        <div className="banner info">
          <span aria-hidden="true">ℹ</span>
          <div>
            No Alpaca paper credentials are configured, so there is no account data and the worker does not trade. You can still learn with a
            labelled SAMPLE replay on <Link to="/research">Research</Link> and <Link to="/learn">Learn</Link>. To connect, follow <Link to="/setup">Setup</Link>.
          </div>
        </div>
      )}

      <div className="grid g4">
        <div className="card tight">
          <div className="kpi-label">Mode</div>
          <div className="kpi-value">{o.mode === "PAPER_AUTO" ? "Paper automation" : "Observe only"}</div>
          <div className="muted small">{o.mode === "PAPER_AUTO" ? <>Enabled <DubaiTime t={o.activated_at} /></> : "No orders are created."}</div>
        </div>
        <div className="card tight">
          <div className="kpi-label">Paper equity</div>
          <div className="kpi-value">{o.account ? <Money v={o.account.equity} /> : <span className="muted">Not connected</span>}</div>
          {o.account && <div className="muted small">As of <DubaiTime t={o.account.as_of} /></div>}
        </div>
        <div className="card tight">
          <div className="kpi-label">Cash (unborrowed)</div>
          <div className="kpi-value">{o.account ? <Money v={o.account.cash} /> : <span className="muted">—</span>}</div>
          {o.account && <div className="muted small" title={o.account.buying_power_note}>Buying power <Money v={o.account.buying_power} /> (may include margin; never used)</div>}
        </div>
        <div className="card tight">
          <div className="kpi-label">SPY rule holding</div>
          <div className="kpi-value num" title={o.strategy_position ? `${o.strategy_position.held_qty} SPY exactly` : undefined}>
            {o.strategy_position ? `${Number(o.strategy_position.held_qty).toFixed(4)} SPY` : "—"}
          </div>
          <HoldingValue symbol="SPY" qty={o.strategy_position?.held_qty} cost={o.strategy_position?.cost_basis?.usd} />
          <div className="muted small">Signal target: {o.strategy_position?.signal_target ?? "—"}</div>
        </div>
        <GoldCard g={o.gold} />
      </div>

      <div style={{ marginTop: "1rem" }}>
        <LivePanel stored={chart.data?.available && chart.data.dataset?.mode === "ALPACA" ? chart.data : null} />
      </div>

      <div className="grid g2" style={{ marginTop: "1rem" }}>
        <div className="card">
          <h2>Latest decision</h2>
          {d ? (
            <>
              <div className="row">
                <StateBadge state={d.action} />
                {d.data_mode === "SAMPLE" && <Badge kind="sample">SAMPLE</Badge>}
                <span className="muted">Session {d.session}</span>
              </div>
              <p>{d.reason}</p>
              <Link to={`/decisions/${d.id}`}>Explain this decision →</Link>
            </>
          ) : (
            <p className="muted">No decision yet. The worker evaluates 15 minutes after each completed session once the paper account is connected.</p>
          )}
          <h3 style={{ marginTop: "1rem" }}>Gold AI</h3>
          {o.gold?.latest ? (
            <>
              <div className="row">
                <StateBadge state={o.gold.latest.action} />
                {o.gold.latest.probability != null && <Badge kind="">{Math.round(o.gold.latest.probability * 100)}% model score</Badge>}
                <span className="muted">Session {o.gold.latest.session}</span>
              </div>
              <p className="small">{o.gold.latest.reason}</p>
              <Link to="/gold">See the signals →</Link>
            </>
          ) : <p className="muted small">No gold decision yet.</p>}
        </div>
        <div className="card">
          <h2>Next action</h2>
          <table>
            <tbody>
              <tr><th scope="row">Next evaluation</th><td><DubaiTime t={o.market.next_evaluation} withNy /></td></tr>
              <tr><th scope="row">Next session open</th><td>{o.market.next ? <>{new Date(o.market.next.open_utc).toLocaleString("en-GB", { timeZone: "Asia/Dubai", weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })} Dubai{o.market.next.early_close && <Badge kind="warn">early close</Badge>}</> : "—"}</td></tr>
              <tr><th scope="row">Pending order</th><td>{nextExec ? <>{nextExec.symbol ?? "SPY"} {nextExec.side} for {nextExec.execution_session} open {nextExec.frozen_notional_money && <Money v={nextExec.frozen_notional_money} />}</> : <span className="muted">None</span>}</td></tr>
              <tr><th scope="row">Pending buy notional</th><td><Money v={o.pending_buy_notional} /></td></tr>
              <tr><th scope="row">Reconciliation</th><td>{o.reconciliation ? (o.reconciliation.ok ? <Badge kind="ok" icon="✔">clean</Badge> : <Badge kind="bad" icon="✖">discrepancies</Badge>) : <span className="muted">not yet run</span>} {o.reconciliation && <DubaiTime t={o.reconciliation.at} />}</td></tr>
              <tr><th scope="row">Worker heartbeat</th><td>{o.worker.text} {o.worker.heartbeat && <DubaiTime t={o.worker.heartbeat} />}</td></tr>
            </tbody>
          </table>
          <p className="muted small">The laptop must be awake and online at the evaluation time and during the first 5 minutes after the open when an order is pending. A sleeping laptop cannot alert you.</p>
        </div>
      </div>

      <div className="grid g2" style={{ marginTop: "1rem" }}>
        <div className="card">
          <h2>Positions at the broker</h2>
          {o.positions.length ? (
            <table>
              <thead><tr><th>Symbol</th><th>Qty</th><th>Avg price (USD)</th><th>Market value (USD)</th></tr></thead>
              <tbody>{o.positions.map((p: any) => <tr key={p.symbol}><td>{p.symbol}</td><td className="num">{p.qty}</td><td className="num">{p.avg_entry_price}</td><td className="num">{p.market_value}</td></tr>)}</tbody>
            </table>
          ) : <p className="muted">{o.account ? "No positions." : "Not connected."}</p>}
          {o.strategy_position?.exposure_note && <p className="small">{o.strategy_position.exposure_note}</p>}
        </div>
        <div className="card">
          <h2>Drawdown review</h2>
          <p className="small muted">{o.drawdown.note}</p>
          {o.drawdown.marks.length ? (
            <table>
              <thead><tr><th>Session</th><th>Equity (USD)</th><th>High-water mark</th><th>Drawdown</th></tr></thead>
              <tbody>{o.drawdown.marks.slice(-5).reverse().map((m: any) => <tr key={m.session}><td>{m.session}</td><td className="num">{Number(m.equity).toFixed(2)}</td><td className="num">{Number(m.hwm).toFixed(2)}</td><td className="num">{pct(m.drawdown)}</td></tr>)}</tbody>
            </table>
          ) : <p className="muted">No close-to-close equity marks yet (recorded only after paper automation is enabled).</p>}
          <p className="small">Warn at {pct(o.drawdown.warn, 0)}; pause new entries at {pct(o.drawdown.pause, 0)}.</p>
        </div>
      </div>

      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>Event inbox</h2>
        {ev.data?.items?.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>When</th><th>Severity</th><th>Event</th></tr></thead>
              <tbody>
                {ev.data.items.slice(0, 12).map((e: any) => (
                  <tr key={e.id}><td><DubaiTime t={e.at} /></td><td><Badge kind={e.severity === "critical" ? "bad" : e.severity === "warning" ? "warn" : "info"}>{e.severity}</Badge></td><td><strong>{e.title}</strong><div className="small muted">{e.body}</div></td></tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="muted">No events yet.</p>}
      </div>
      <p className="muted small">AED amounts use the reference rate {o.fx.rate} ({o.fx.source})</p>
    </div>
  );
}

/** Gold AI at a glance: on/shadow, holding, live value, latest call. */
function GoldCard({ g }: { g: any }) {
  if (!g) return null;
  const held = Number(g.held_qty || 0);
  const L = g.latest;
  return (
    <div className="card tight">
      <div className="kpi-label"><Link to="/gold">Gold AI holding</Link></div>
      <div className="kpi-value num">{held > 0 ? `${held.toFixed(4)} GLD` : "No gold"}</div>
      {held > 0 && <HoldingValue symbol="GLD" qty={g.held_qty} cost={g.cost_basis?.usd} />}
      <div className="muted small">
        {g.settings.enabled ? "AI trading on" : "Shadow mode"} · USD {Number(g.settings.budget_usd).toFixed(0)} per buy
        {L?.probability != null && <> · last call {Math.round(L.probability * 100)}% up ({L.action})</>}
      </div>
      {g.pending && <div className="small">Order waiting: {g.pending.side} at {g.pending.execution_session} open</div>}
    </div>
  );
}

/** Live value of a strategy holding and its unrealised profit/loss vs what was paid (display only, IEX price). */
function HoldingValue({ symbol, qty, cost }: { symbol: string; qty?: string; cost?: string }) {
  const q = useQuote(symbol);
  const n = Number(qty || 0);
  if (!q || !n || !cost) return null;
  const value = n * q.price;
  const pl = value - Number(cost);
  const up = pl >= 0;
  return (
    <div className="small">
      Worth ≈ USD {value.toFixed(2)} · paid {Number(cost).toFixed(2)} ·{" "}
      <span className={`chg ${up ? "up" : "down"}`}>{up ? "▲ +" : "▼ "}{pl.toFixed(2)} USD ({up ? "+" : ""}{((pl / Number(cost)) * 100).toFixed(2)}%)</span>
      <span className="muted"> ≈ AED {(pl * 3.6725).toFixed(2)}</span>
    </div>
  );
}
