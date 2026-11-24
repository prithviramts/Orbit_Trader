import { Link } from "react-router-dom";
import { Badge, ErrorNote, Loading, StateBadge, UsdAed, usePoll } from "../components";

function Change({ usd }: { usd: string | null | undefined }) {
  if (usd === null || usd === undefined) return <span className="muted">N/A</span>;
  const n = Number(usd);
  return <span className={n > 0 ? "chg up" : n < 0 ? "chg down" : ""}>{n > 0 ? "+" : ""}<UsdAed usd={usd} /></span>;
}

const STATUS_KIND: Record<string, string> = { on: "ok", off: "", paused: "warn" };
const NAMES: Record<string, string> = { SPY: "SPY (US stocks)", GLD: "GLD (gold)", IVV: "IVV (US stocks, day trader)" };

/** One screen for a quick daily check: money, what each strategy is doing, today's orders, problems, what's next. */
export function Today() {
  const { data: t, error } = usePoll<any>("/api/today", 20000);
  if (error) return <ErrorNote error={error} />;
  if (!t) return <Loading />;
  const m = t.money;
  return (
    <div className="today">
      <h1>Today</h1>

      <div className={`card status-hero today-hero ${t.level === "ok" ? "is-ok" : t.level === "bad" ? "is-bad" : "is-warn"}`} role="status" aria-live="polite">
        <div className="status-big">{t.level === "ok" ? "✓ " : t.level === "bad" ? "⚠ " : "• "}{t.headline}</div>
        {t.checked && <p className="small muted">Last account check: {t.checked.at} Dubai, {t.checked.ok ? "everything matched" : "something did not match"}.</p>}
        {t.attention.map((a: any, i: number) => (
          <div key={i} className={`banner ${a.level}`}><span aria-hidden="true">!</span><div>{a.text}</div></div>
        ))}
        {t.alerts.length > 0 && (
          <details className="small">
            <summary>{t.alerts.length} unread warning{t.alerts.length > 1 ? "s" : ""} in the last 24 hours</summary>
            <ul>{t.alerts.map((a: any, i: number) => <li key={i}>{a.at}: {a.title}</li>)}</ul>
            <p className="muted">Open the bell at the top to read and clear them.</p>
          </details>
        )}
      </div>

      {m ? (
        <div className="grid g4">
          <div className="card tight"><div className="kpi-label">Paper account</div><div className="kpi-value"><UsdAed usd={m.equity} /></div></div>
          <div className="card tight"><div className="kpi-label">Today</div><div className="kpi-value"><Change usd={m.today} /></div></div>
          <div className="card tight"><div className="kpi-label">Since Orbit started</div><div className="kpi-value"><Change usd={m.since_start} /></div></div>
          <div className="card tight"><div className="kpi-label">Invested now</div><div className="kpi-value"><UsdAed usd={m.invested} /></div>
            <div className="small muted">The rest is cash.</div></div>
        </div>
      ) : <div className="banner warn"><span aria-hidden="true">!</span><div>No account data yet: Orbit has not reached Alpaca.</div></div>}

      {m && m.holdings.length > 0 && (
        <div className="card">
          <h2>What you hold</h2>
          <div className="table-wrap"><table className="small">
            <thead><tr><th>Fund</th><th>Shares</th><th>Worth now</th><th>Paid</th><th>Gain / loss</th></tr></thead>
            <tbody>{m.holdings.map((h: any) => (
              <tr key={h.symbol}><th scope="row">{NAMES[h.symbol] ?? h.symbol}</th><td className="num">{Number(h.qty).toFixed(4)}</td>
                <td><UsdAed usd={h.value} /></td><td><UsdAed usd={h.cost} /></td><td><Change usd={h.gain} /></td></tr>
            ))}</tbody>
          </table></div>
        </div>
      )}

      <h2 style={{ marginTop: "1.2rem" }}>Strategies</h2>
      <div className="grid g3 today-strategies">
        {t.strategies.map((st: any) => (
          <div key={st.symbol} className="card">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <strong>{st.name}</strong>
              <Badge kind={STATUS_KIND[st.status] ?? ""}>{st.status_text}</Badge>
            </div>
            <p className="small muted">{st.about}</p>
            {st.latest ? (
              <>
                <div className="row">
                  <StateBadge state={st.latest.action} />
                  {st.latest.score != null && <Badge kind="">{Math.round(st.latest.score * 100)}% model score</Badge>}
                  <span className="small muted">{st.latest.session}</span>
                </div>
                <p className="small" title={st.latest.reason}>{st.latest.summary}</p>
              </>
            ) : <p className="small muted">No decision yet.</p>}
            {Number(st.held_qty) > 0 && <p className="small">Holding {Number(st.held_qty).toFixed(4)} {st.symbol}.</p>}
            {st.pending && <p className="small"><Badge kind="info">Planned</Badge> {st.pending.side.toLowerCase()} {st.pending.amount ? `USD ${st.pending.amount}` : `${st.pending.qty} shares`} on {st.pending.when}</p>}
          </div>
        ))}
      </div>

      <div className="grid g2" style={{ marginTop: "1rem" }}>
        <div className="card">
          <h2>Orders on {t.orders.day}</h2>
          {t.orders.items.length ? (
            <ul className="plain">{t.orders.items.map((o: any, i: number) => (
              <li key={i}>
                <strong>{o.side} {o.symbol}</strong> <StateBadge state={o.state} />{" "}
                {o.price ? <>at USD {Number(o.price).toFixed(2)}</> : o.amount ? <>USD {o.amount}</> : null}
                {o.at && <span className="muted small"> · {o.at} Dubai</span>}
                {o.why && <div className="small muted">{o.why}</div>}
              </li>
            ))}</ul>
          ) : <p className="muted">No orders on this trading day.</p>}
          <Link to="/orders">All trades →</Link>
        </div>
        <div className="card">
          <h2>What happens next</h2>
          <ul>{t.next.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul>
          <p className="small muted">Keep this computer awake from 18:30 to about 01:00 Dubai on trading days so orders and the day trader's safety exits run.</p>
          <Link to="/overview">Detailed overview →</Link>
        </div>
      </div>
      <p className="small muted" style={{ marginTop: "1rem" }}>Paper money only. Amounts in USD with the AED reference value.</p>
    </div>
  );
}
