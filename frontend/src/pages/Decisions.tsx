import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { post } from "../api";
import { Badge, Confirm, DubaiTime, ErrorNote, Loading, Money, StateBadge, usePoll } from "../components";

export function Decisions() {
  const [q, setQ] = useState("");
  const [action, setAction] = useState("");
  const { data, error } = usePoll<any>(`/api/decisions?q=${encodeURIComponent(q)}&action=${action}`, 30000);
  const nav = useNavigate();
  return (
    <div>
      <h1>Decisions</h1>
      <p className="muted">Every evaluation is stored with the data it used. BUY/SELL create an order intent; HOLD/SKIP explain why nothing was ordered.</p>
      <div className="row" style={{ marginBottom: ".75rem" }}>
        <label htmlFor="q" className="sr-only">Search</label>
        <input id="q" placeholder="Search reasons…" value={q} onChange={(e) => setQ(e.target.value)} />
        <label htmlFor="act" className="sr-only">Action</label>
        <select id="act" value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">All actions</option>
          {["BUY", "SELL", "HOLD", "SKIP", "ERROR"].map((a) => <option key={a}>{a}</option>)}
        </select>
        <a className="btn" href="/api/export/decisions">Export JSON</a>
      </div>
      <ErrorNote error={error} />
      {!data ? <Loading /> : data.items.length === 0 ? (
        <div className="card"><p className="muted">No live decisions yet. They appear after the worker connects to the paper account and a session completes. Backtest decisions live on the Research page.</p></div>
      ) : (
        <div className="card table-wrap">
          <table>
            <thead><tr><th>Session</th><th>Action</th><th>Target</th><th>Close / avg (USD)</th><th>Mode</th><th>Reason</th></tr></thead>
            <tbody>
              {data.items.map((d: any) => (
                <tr key={d.id} className="clickable" onClick={() => nav(`/decisions/${d.id}`)}>
                  <td><Link to={`/decisions/${d.id}`}>{d.session}{d.sequence > 1 ? ` (#${d.sequence})` : ""}</Link></td>
                  <td><StateBadge state={d.action} /></td>
                  <td>{d.target}</td>
                  <td className="num">{d.close ?? "—"} / {d.sma ? Number(d.sma).toFixed(2) : "—"}</td>
                  <td>{d.mode_at_decision}</td>
                  <td className="small">{d.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">{data.total} decision(s).</p>
        </div>
      )}
    </div>
  );
}

export function DecisionDetail() {
  const { id } = useParams();
  const { data, error } = usePoll<any>(`/api/decisions/${id}`, 30000);
  const [showInputs, setShowInputs] = useState(false);
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading />;
  const d = data.decision;
  const sig = d.inputs?.signal;
  return (
    <div>
      <p><Link to="/decisions">← All decisions</Link></p>
      <h1>Decision for {d.session} <StateBadge state={d.action} /></h1>
      <div className="row" style={{ marginBottom: "1rem" }}>
        <Badge kind={d.data_mode === "SAMPLE" ? "sample" : "accent"}>{d.data_mode}</Badge>
        <Badge kind="">Strategy v{d.strategy_version_id}</Badge>
        <Badge kind="">Mode {d.mode_at_decision}</Badge>
        <span className="muted small">Known at <DubaiTime t={d.known_at} withNy /></span>
      </div>
      <details open>
        <summary>Explain this decision</summary>
        <ul className="explain">{data.explanation.map((l: string, i: number) => <li key={i}>{l}</li>)}</ul>
      </details>
      <div className="grid g2">
        <div className="card">
          <h2>Inputs</h2>
          <table>
            <tbody>
              <tr><th scope="row">Close</th><td><Money v={d.close_money} /></td></tr>
              <tr><th scope="row">200-session average</th><td><Money v={d.sma_money} /></td></tr>
              <tr><th scope="row">Comparison</th><td>{d.comparison}</td></tr>
              <tr><th scope="row">Window</th><td>{d.window_count} closes{sig?.window_first_session ? ` from ${sig.window_first_session}` : ""}</td></tr>
              <tr><th scope="row">Previous → new target</th><td>{d.previous_target} → {d.target}</td></tr>
              <tr><th scope="row">Held (confirmed fills)</th><td className="num">{d.held_qty_at_decision} SPY</td></tr>
              <tr><th scope="row">Data source</th><td className="small">{d.data_source}</td></tr>
              <tr><th scope="row">Data fingerprint</th><td className="mono small">{d.data_fingerprint || "—"}</td></tr>
              <tr><th scope="row">Fetched</th><td><DubaiTime t={d.data_fetched_at} /></td></tr>
            </tbody>
          </table>
          {d.inputs?.sizing && (
            <>
              <h3>Entry sizing</h3>
              <table><tbody>
                <tr><th scope="row">25% of equity</th><td>{Number(d.inputs.sizing.by_fraction).toFixed(2)} USD</td></tr>
                <tr><th scope="row">Hard cap</th><td>{Number(d.inputs.sizing.by_cap).toFixed(2)} USD</td></tr>
                <tr><th scope="row">Cash after reserve</th><td>{Number(d.inputs.sizing.by_cash).toFixed(2)} USD</td></tr>
                <tr><th scope="row">Target (limited by)</th><td>{Number(d.inputs.sizing.target).toFixed(2)} USD ({d.inputs.sizing.limited_by})</td></tr>
              </tbody></table>
            </>
          )}
          {sig?.inputs && (
            <p><button onClick={() => setShowInputs(!showInputs)}>{showInputs ? "Hide" : "Show"} all {sig.inputs.length} closes used</button></p>
          )}
          {showInputs && <div className="table-wrap" style={{ maxHeight: 300, overflowY: "auto" }}><table><thead><tr><th>Session</th><th>Close</th></tr></thead><tbody>{sig.inputs.map((r: string[]) => <tr key={r[0]}><td>{r[0]}</td><td className="num">{r[1]}</td></tr>)}</tbody></table></div>}
        </div>
        <div className="card">
          <h2>Decision → order → fill timeline</h2>
          <ol className="timeline">{data.timeline.map((t: any, i: number) => <li key={i}><div className="small muted"><DubaiTime t={t.at} /></div><Badge kind="">{t.kind}</Badge> {t.text}</li>)}</ol>
          {data.intents.map((i: any) => <IntentCard key={i.id} i={i} />)}
          {data.fills.length > 0 && (
            <>
              <h3>Fills</h3>
              <div className="table-wrap"><table><thead><tr><th>Side</th><th>Qty</th><th>Price</th><th>Value</th><th>Broker order</th></tr></thead>
                <tbody>{data.fills.map((f: any) => <tr key={f.id}><td>{f.side}</td>
                  <td className="num" title={`${f.qty} SPY exactly`}>{Number(f.qty).toFixed(4)}</td>
                  <td><Money v={f.price_money} /></td><td><Money v={f.notional_money} /></td>
                  <td className="mono small" title={f.broker_order_id}>{String(f.broker_order_id).slice(0, 8)}…</td></tr>)}</tbody></table></div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function IntentCard({ i }: { i: any }) {
  return (
    <div className="card tight" style={{ marginTop: ".75rem" }}>
      <div className="row"><strong>Intent #{i.id}</strong> <StateBadge state={i.state} /> <Badge kind="">{i.kind} {i.side}</Badge></div>
      <table className="small"><tbody>
        <tr><th scope="row">Execution session</th><td>{i.execution_session} (attempt {i.attempt})</td></tr>
        <tr><th scope="row">Client order id</th><td className="mono">{i.client_order_id}</td></tr>
        <tr><th scope="row">Broker order id</th><td className="mono">{i.broker_order_id || "—"} {i.broker_status && `(${i.broker_status})`}</td></tr>
        {i.frozen_notional_money && <tr><th scope="row">Frozen target</th><td><Money v={i.frozen_notional_money} /></td></tr>}
        {i.submitted_notional_money && <tr><th scope="row">Submitted</th><td><Money v={i.submitted_notional_money} /></td></tr>}
        {i.target_qty && <tr><th scope="row">Sell qty</th><td className="num">{i.target_qty}</td></tr>}
        <tr><th scope="row">Filled</th><td className="num">{i.filled_qty} {i.filled_avg_price && `@ avg ${Number(i.filled_avg_price).toFixed(4)} USD`}</td></tr>
        {i.parent_intent_id && <tr><th scope="row">Lineage</th><td>follows intent #{i.parent_intent_id}</td></tr>}
        <tr><th scope="row">Reason</th><td>{i.reason}</td></tr>
        {i.sizing?.reduced_at_execution && <tr><th scope="row">Reduced</th><td>{i.sizing.reduced_at_execution}</td></tr>}
      </tbody></table>
    </div>
  );
}

export function Orders() {
  const { data, error, reload } = usePoll<any>("/api/orders", 15000);
  const ov = usePoll<any>("/api/overview", 15000);
  const strat = usePoll<any>("/api/strategy", 0);
  const [modal, setModal] = useState<string | null>(null);
  const [ackId, setAckId] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const desc = strat.data?.control_descriptions || {};
  const o = ov.data;
  const act = async (path: string, body?: any) => {
    const r: any = await post(path, body);
    setMsg(r.message || "Done.");
    reload();
    ov.reload();
  };
  return (
    <div>
      <h1>Orders &amp; positions</h1>
      {msg && <div className="banner info" role="status"><span aria-hidden="true">ℹ</span><div>{msg}</div></div>}
      <div className="card">
        <h2>Controls</h2>
        <p className="small muted">Each button explains its effect before anything happens. Pausing never closes positions or cancels broker orders by itself.</p>
        <div className="row">
          {o?.mode === "PAPER_AUTO" ? <button onClick={() => setModal("observe")}>Return to OBSERVE</button> : <Link className="btn" to="/setup">Enable paper automation (Setup)</Link>}
          {o?.flags.new_orders_paused ? <button onClick={() => setModal("resume")}>Resume new orders</button> : <button onClick={() => setModal("pause")}>Pause new orders</button>}
          <button onClick={() => setModal("cancel_pending")}>Cancel pending strategy orders</button>
          <button className="danger" onClick={() => setModal("close_position")}>Close strategy position (paper)</button>
        </div>
      </div>
      {modal && (
        <Confirm
          title={{ observe: "Return to OBSERVE", pause: "Pause new orders", resume: "Resume new orders", cancel_pending: "Cancel pending strategy orders", close_position: "Close strategy position" }[modal]!}
          needPassword={modal === "close_position"} danger={modal === "close_position"} onClose={() => setModal(null)}
          onConfirm={async (pw) => {
            const path = { observe: "/api/controls/observe", pause: "/api/controls/pause", resume: "/api/controls/resume", cancel_pending: "/api/controls/cancel-pending", close_position: "/api/controls/close-position" }[modal]!;
            await act(path, modal === "close_position" ? { password: pw } : undefined);
          }}>
          <p>{desc[modal] || ""}</p>
        </Confirm>
      )}
      <div className="grid g2" style={{ marginTop: "1rem" }}>
        <div className="card">
          <h2>Surviving broker positions</h2>
          {o?.positions?.length ? <table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg (USD)</th></tr></thead><tbody>{o.positions.map((p: any) => <tr key={p.symbol}><td>{p.symbol}</td><td className="num">{p.qty}</td><td className="num">{p.avg_entry_price}</td></tr>)}</tbody></table> : <p className="muted">{o?.account ? "None." : "Not connected."}</p>}
          <p className="small">Strategy-confirmed holding: <strong className="num">{o?.strategy_position?.held_qty ?? "—"} SPY</strong></p>
        </div>
        <div className="card">
          <h2>Open intents / working orders</h2>
          {o?.open_intents?.length ? o.open_intents.map((i: any) => <div key={i.id}><StateBadge state={i.state} /> #{i.id} {i.kind} {i.side} for {i.execution_session}</div>) : <p className="muted">None.</p>}
          <p className="small muted">Broker-accepted orders and positions survive if this app or the worker stops. A five-minute submission window does not guarantee a fill within five minutes.</p>
        </div>
      </div>
      <ErrorNote error={error} />
      <div className="card table-wrap" style={{ marginTop: "1rem" }}>
        <h2>Order intents</h2>
        {!data ? <Loading /> : data.intents.length === 0 ? <p className="muted">No intents yet.</p> : (
          <table>
            <thead><tr><th>#</th><th>State</th><th>Kind</th><th>Exec. session</th><th>Amount</th><th>Filled</th><th>Client id</th><th></th></tr></thead>
            <tbody>
              {data.intents.map((i: any) => (
                <tr key={i.id}>
                  <td>{i.decision_id ? <Link to={`/decisions/${i.decision_id}`}>{i.id}</Link> : i.id}</td>
                  <td><StateBadge state={i.state} />{i.review_required && <Badge kind="bad">review</Badge>}</td>
                  <td>{i.kind} {i.side}</td>
                  <td>{i.execution_session}</td>
                  <td>{i.side === "BUY" ? <Money v={i.submitted_notional_money || i.frozen_notional_money} /> : <span className="num">{i.target_qty} SPY</span>}</td>
                  <td className="num">{i.filled_qty}</td>
                  <td className="mono small">{i.client_order_id}</td>
                  <td>{i.review_required && <button onClick={() => setAckId(i)}>Review…</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {ackId && <ReviewModal intent={ackId} onClose={() => { setAckId(null); reload(); ov.reload(); }} />}
    </div>
  );
}

function ReviewModal({ intent, onClose }: { intent: any; onClose: () => void }) {
  const [createNew, setCreateNew] = useState(false);
  const [absent, setAbsent] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="modal-back" role="dialog" aria-modal="true" aria-label="Review intent">
      <div className="card modal">
        <h2>Review intent #{intent.id} ({intent.state.replaceAll("_", " ")})</h2>
        <p>{intent.reason}</p>
        <p className="small">Acknowledging marks this intent SUPERSEDED (its history is kept). The bot never retries automatically just because the signal still says the same thing.</p>
        {intent.state === "AMBIGUOUS" && (
          <label className="row"><input type="checkbox" checked={absent} onChange={(e) => setAbsent(e.target.checked)} /> I checked the Alpaca dashboard directly and no order with client id <code>{intent.client_order_id}</code> exists.</label>
        )}
        {intent.side === "BUY" && (
          <label className="row"><input type="checkbox" checked={createNew} onChange={(e) => setCreateNew(e.target.checked)} /> Create a new entry intent from the latest completed signal for the next eligible open (if still INVESTED and flat).</label>
        )}
        <ErrorNote error={err} />
        <div className="row" style={{ marginTop: ".75rem" }}>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={async () => { try { await post("/api/controls/ack-review", { intent_id: intent.id, create_new: createNew, confirm_absent_at_broker: absent }); onClose(); } catch (e: any) { setErr(e.message); } }}>Acknowledge</button>
        </div>
      </div>
    </div>
  );
}
