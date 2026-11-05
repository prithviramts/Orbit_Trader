import { Link } from "react-router-dom";
import { Badge, DubaiTime, ErrorNote, Loading, Money, ModeBadge, StateBadge, usePoll } from "../components";

/** Phone-first, read-only "is everything OK?" screen. Bookmark /status on your phone. */
export function Status() {
  const { data: o, error } = usePoll<any>("/api/overview", 30000);
  if (error) return <ErrorNote error={error} />;
  if (!o) return <Loading />;
  const ok = o.incidents.length === 0 && o.worker.state === "RUNNING";
  const pending = o.open_intents.filter((i: any) => i.state === "PENDING");
  const working = o.open_intents.filter((i: any) => i.state !== "PENDING");
  const d = o.last_decision;
  return (
    <div className="status-page">
      <div className={`card status-hero ${ok ? "is-ok" : "is-bad"}`} role="status" aria-live="polite">
        <div className="status-big">
          <span aria-hidden="true">{ok ? "✔" : "!"}</span> {ok ? "All good" : `${o.incidents.length} thing(s) need attention`}
        </div>
        <div className="row"><ModeBadge mode={o.mode} /><Badge kind={o.worker.state === "RUNNING" ? "ok" : "bad"}>Worker {o.worker.state.replaceAll("_", " ").toLowerCase()}</Badge></div>
        <div className="small muted">Updated <DubaiTime t={o.market.now} /></div>
      </div>

      {o.incidents.map((x: any, i: number) => (
        <div key={i} className={`banner ${x.severity === "critical" ? "bad" : "warn"}`} role="alert">
          <span aria-hidden="true">{x.severity === "critical" ? "⛔" : "!"}</span>
          <div><strong>{x.kind.replaceAll("_", " ")}</strong><div className="small">{x.text}</div></div>
        </div>
      ))}

      <div className="card">
        <h2>Next</h2>
        {pending.length ? pending.map((i: any) => (
          <p key={i.id}><strong>{i.side}</strong> at the {i.execution_session} open {i.frozen_notional_money && <Money v={i.frozen_notional_money} />}. Be online for the first 5 minutes.</p>
        )) : <p>No order planned.</p>}
        {working.map((i: any) => <p key={i.id}><StateBadge state={i.state} /> {i.kind} {i.side} — check it resolves.</p>)}
        <p className="small">Next decision: <DubaiTime t={o.market.next_evaluation} /></p>
      </div>

      <div className="card">
        <h2>Last decision</h2>
        {d ? (
          <>
            <p><StateBadge state={d.action} /> <span className="muted small">{d.session}</span></p>
            <p className="small">{d.reason}</p>
            <Link to={`/decisions/${d.id}`}>Explain →</Link>
          </>
        ) : <p className="muted">None yet.</p>}
      </div>

      <div className="card">
        <h2>Paper account</h2>
        {o.account ? (
          <table><tbody>
            <tr><th scope="row">Equity</th><td><Money v={o.account.equity} /></td></tr>
            <tr><th scope="row">Cash</th><td><Money v={o.account.cash} /></td></tr>
            <tr><th scope="row">Strategy holds</th><td className="num">{o.strategy_position?.held_qty ?? "0"} SPY</td></tr>
          </tbody></table>
        ) : <p className="muted">Not connected.</p>}
        <p className="small muted">Read-only screen. Controls are on <Link to="/orders">Orders</Link> and need your password.</p>
      </div>
    </div>
  );
}
