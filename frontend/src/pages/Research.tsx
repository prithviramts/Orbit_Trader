import { useEffect, useMemo, useState } from "react";
import { get, post, postForm } from "../api";
import { Badge, ErrorNote, Loading, pct, SampleBanner, usePoll, UsdAed } from "../components";
import { DrawdownChart, IndexedChart, Marker, PriceChart } from "../charts";

const METRIC_HELP: Record<string, string> = {
  net_return: "Ending equity ÷ starting equity − 1. Not annualised.",
  ending_equity: "Cash + market value of holdings at the last close.",
  max_drawdown: "Largest fall from a previous peak, close to close.",
  time_in_market: "Share of sessions with any SPY held.",
  average_exposure: "Average share of equity invested in SPY.",
  turnover: "Total traded value ÷ average equity. Higher means more trading costs.",
  completed_round_trips: "Number of buy-then-sell cycles finished. Few round trips = weak evidence.",
  estimated_costs: "Fees plus assumed slippage versus the open price.",
  annualized_return: "Only shown for ≥ 252 sessions; still an approximation.",
};
const NAMES: Record<string, string> = { strategy: "Strategy (SMA rule + risk overlay)", matched_entry_benchmark: "Matched-entry buy & hold", full_investment_benchmark: "~100% buy & hold" };

export function Research() {
  const ds = usePoll<any>("/api/datasets", 0);
  const runs = usePoll<any>("/api/backtests", 5000);
  const [sel, setSel] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState<any>({ dataset_id: "", period: "development", starting_cash: "1000", slippage_bps: "5", fee_per_order_usd: "0", confirm_holdout: false });
  useEffect(() => {
    if (!form.dataset_id && ds.data?.items?.length) setForm((f: any) => ({ ...f, dataset_id: String(ds.data.items[0].id) }));
  }, [ds.data]);
  const run = async () => {
    setErr(null);
    try {
      const r: any = await post("/api/backtests", { ...form, dataset_id: Number(form.dataset_id) });
      setSel(r.run_id);
      runs.reload();
    } catch (e: any) { setErr(e.message); }
  };
  return (
    <div>
      <h1>Research</h1>
      <p className="muted">Backtests run in a separate bounded job queue so they cannot slow the trading worker. Every attempted configuration is recorded below.</p>
      <div className="grid g2">
        <div className="card">
          <h2>1. Choose data</h2>
          <div className="row">
            <button onClick={async () => { setErr(null); try { await post("/api/datasets/sample"); ds.reload(); } catch (e: any) { setErr(e.message); } }}>Create SAMPLE dataset (synthetic)</button>
            <button onClick={async () => { setErr(null); try { const r: any = await post("/api/datasets/fetch-alpaca"); ds.reload(); alert(`Fetched ${r.bars} sessions ${r.first} → ${r.last}`); } catch (e: any) { setErr(e.message); } }}>Fetch SPY history from Alpaca</button>
          </div>
          <CsvImport onDone={ds.reload} onErr={setErr} />
          {ds.data?.items?.length ? (
            <table className="small" style={{ marginTop: ".75rem" }}>
              <thead><tr><th>#</th><th>Mode</th><th>Range</th><th>Source</th></tr></thead>
              <tbody>{ds.data.items.map((d: any) => <tr key={d.id}><td>{d.id}</td><td><Badge kind={d.mode === "SAMPLE" ? "sample" : "accent"}>{d.mode}</Badge></td><td>{d.describe.first_session} → {d.describe.last_session} ({d.describe.bar_count})</td><td>{d.source}{d.quality?.missing_sessions?.length ? ` · ${d.quality.missing_sessions.length} missing sessions` : ""}</td></tr>)}</tbody>
            </table>
          ) : <p className="muted">No datasets yet.</p>}
        </div>
        <div className="card">
          <h2>2. Run a backtest</h2>
          <div className="field"><label htmlFor="ds">Dataset</label>
            <select id="ds" value={form.dataset_id} onChange={(e) => setForm({ ...form, dataset_id: e.target.value })}>
              {ds.data?.items?.map((d: any) => <option key={d.id} value={d.id}>#{d.id} {d.mode}</option>)}
            </select></div>
          <div className="field"><label htmlFor="per">Period (chronological split)</label>
            <select id="per" value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value })}>
              <option value="development">Development (first 60%)</option>
              <option value="validation">Validation (next 20%)</option>
              <option value="test_holdout">Final holdout (last 20%) — use rarely</option>
              <option value="full">Full range</option>
            </select></div>
          {form.period === "test_holdout" && <label className="row small"><input type="checkbox" checked={form.confirm_holdout} onChange={(e) => setForm({ ...form, confirm_holdout: e.target.checked })} /> Settings are fixed; I understand repeatedly checking the holdout turns it into training data.</label>}
          <div className="row">
            <div className="field"><label htmlFor="sc">Starting cash (USD)</label><input id="sc" size={8} value={form.starting_cash} onChange={(e) => setForm({ ...form, starting_cash: e.target.value })} /> <span className="small muted">≈ AED {(Number(form.starting_cash) * 3.6725).toFixed(2)}</span></div>
            <div className="field"><label htmlFor="sl">Slippage (bps)</label><input id="sl" size={4} value={form.slippage_bps} onChange={(e) => setForm({ ...form, slippage_bps: e.target.value })} /></div>
            <div className="field"><label htmlFor="fee">Fee/order (USD)</label><input id="fee" size={4} value={form.fee_per_order_usd} onChange={(e) => setForm({ ...form, fee_per_order_usd: e.target.value })} /></div>
          </div>
          <p className="small muted">USD 1,000 (AED 3,672.50) is a suggested virtual research balance, not a recommendation.</p>
          <button className="primary" disabled={!form.dataset_id} onClick={run}>Run backtest</button>
        </div>
      </div>
      <ErrorNote error={err} />
      <div className="card table-wrap" style={{ marginTop: "1rem" }}>
        <h2>Recorded runs</h2>
        {!runs.data ? <Loading /> : (
          <table className="small">
            <thead><tr><th>#</th><th>Status</th><th>Data</th><th>Period</th><th>Strategy return</th><th>Matched B&amp;H</th><th>~100% B&amp;H</th><th></th></tr></thead>
            <tbody>{runs.data.items.map((r: any) => (
              <tr key={r.id} className="clickable" onClick={() => setSel(r.id)}>
                <td>{r.id}</td><td>{r.status}</td><td><Badge kind={r.data_mode === "SAMPLE" ? "sample" : "accent"}>{r.data_mode}</Badge></td><td>{r.period}</td>
                <td className="num">{pct(r.summary?.strategy?.net_return)}</td><td className="num">{pct(r.summary?.matched_entry_benchmark?.net_return)}</td><td className="num">{pct(r.summary?.full_investment_benchmark?.net_return)}</td>
                <td><button onClick={() => setSel(r.id)}>Open</button></td>
              </tr>))}</tbody>
          </table>
        )}
      </div>
      {sel && <RunDetail id={sel} />}
    </div>
  );
}

function CsvImport({ onDone, onErr }: { onDone: () => void; onErr: (e: string | null) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [basis, setBasis] = useState("split_adjusted");
  const [src, setSrc] = useState("");
  return (
    <details style={{ marginTop: ".75rem" }}>
      <summary>Import historical CSV (documented provenance)</summary>
      <p className="small">Columns: <code>date,open,high,low,close[,volume]</code>, daily SPY regular sessions, max 5 MB. Describe exactly where the file came from.</p>
      <input type="file" accept=".csv,text/csv" aria-label="CSV file" onChange={(e) => setFile(e.target.files?.[0] || null)} />
      <div className="field"><label htmlFor="basis">Price basis</label><select id="basis" value={basis} onChange={(e) => setBasis(e.target.value)}><option value="split_adjusted">Split-adjusted</option><option value="raw">Raw (unadjusted)</option></select></div>
      <div className="field"><label htmlFor="src">Source description</label><input id="src" style={{ width: "100%" }} value={src} onChange={(e) => setSrc(e.target.value)} placeholder="e.g. Exported from <provider> on 2026-11-05, split-adjusted, no dividends" /></div>
      <button disabled={!file || src.length < 3} onClick={async () => {
        onErr(null);
        const f = new FormData(); f.append("file", file!); f.append("price_basis", basis); f.append("source_description", src);
        try { await postForm("/api/datasets/import", f); onDone(); } catch (e: any) { onErr(e.message); }
      }}>Import</button>
    </details>
  );
}

function RunDetail({ id }: { id: number }) {
  const [r, setR] = useState<any>(null);
  const [hl, setHl] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      const x: any = await get(`/api/backtests/${id}`);
      if (!alive) return;
      setR(x);
      if (x.status === "QUEUED" || x.status === "RUNNING") setTimeout(load, 700);
    };
    load();
    return () => { alive = false; };
  }, [id]);
  const res = r?.result;
  const markers = useMemo(() => {
    if (!res?.ok) return { s: [] as Marker[], f: [] as Marker[] };
    const s = res.decisions.filter((d: any) => d.action === "BUY" || d.action === "SELL").map((d: any) => ({ day: d.session, kind: d.action, ref: d.ref, label: `${d.action} signal` }));
    const f = res.portfolios.strategy.trades.map((t: any) => ({ day: t.day, kind: t.side, ref: `${t.decision_ref}`, label: `${t.side} ${Number(t.qty).toFixed(4)} @ ${Number(t.price).toFixed(2)}` }));
    return { s, f };
  }, [res]);
  if (!r) return <Loading />;
  if (r.status !== "DONE") return <div className="card" style={{ marginTop: "1rem" }}><p>Run #{id}: {r.status} {r.error}</p></div>;
  if (!res.ok) return <ErrorNote error={res.error} />;
  const P = res.portfolios;
  const priceBars = res.decisions.map((d: any) => ({ day: d.session, close: d.close, sma200: d.sma }));
  const metrics = ["net_return", "ending_equity", "max_drawdown", "time_in_market", "average_exposure", "turnover", "completed_round_trips", "estimated_costs", "annualized_return"];
  return (
    <div style={{ marginTop: "1rem" }}>
      <h2>Run #{id} — {res.data.mode} · {res.period} · {res.range.first_session} → {res.range.last_session}</h2>
      {res.data.mode === "SAMPLE" && <SampleBanner />}
      <div className="row" style={{ marginBottom: ".75rem" }}>
        <a className="btn" href={`/api/backtests/${id}/export?fmt=json`}>Export JSON (with assumptions)</a>
        <a className="btn" href={`/api/backtests/${id}/export?fmt=csv`}>Export trades CSV</a>
      </div>
      <div className="card table-wrap">
        <h2>Results vs benchmarks (same dates, capital and cost assumptions)</h2>
        <table>
          <thead><tr><th>Metric</th>{Object.keys(P).map((k) => <th key={k}>{NAMES[k]}</th>)}</tr></thead>
          <tbody>
            {metrics.map((m) => (
              <tr key={m}>
                <th scope="row" title={METRIC_HELP[m]}><span className="tip">{m.replaceAll("_", " ")}</span></th>
                {Object.keys(P).map((k) => {
                  const v = P[k].metrics[m];
                  let cell: any = v ?? "N/A";
                  if (v !== null && v !== undefined) {
                    if (["net_return", "max_drawdown", "time_in_market", "average_exposure", "annualized_return"].includes(m)) cell = pct(v);
                    else if (["ending_equity", "estimated_costs"].includes(m)) cell = <UsdAed usd={v} />;
                    else if (m === "turnover") cell = Number(v).toFixed(2) + "×";
                  } else if (m === "annualized_return") cell = <span className="muted small">{P[k].metrics.annualized_note}</span>;
                  return <td key={k} className="num">{cell}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="small muted">Only the strategy applies the drawdown review overlay. {P.strategy.metrics.completed_round_trips} completed round trips over {P.strategy.metrics.sessions} sessions — {P.strategy.metrics.completed_round_trips < 10 ? "a small sample; treat differences as weak evidence." : "still one historical path; it does not prove future performance."}</p>
      </div>
      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>Indexed portfolio value (start = 100)</h2>
        <IndexedChart series={[
          { name: "Strategy", color: "var(--chart-sma)", points: P.strategy.equity },
          { name: "Matched-entry B&H", color: "var(--chart-b1)", points: P.matched_entry_benchmark.equity },
          { name: "~100% B&H", color: "var(--chart-b2)", points: P.full_investment_benchmark.equity },
        ]} />
      </div>
      <div className="grid g2" style={{ marginTop: "1rem" }}>
        <div className="card"><h2>Strategy drawdown</h2><DrawdownChart points={P.strategy.drawdown} /></div>
        <div className="card">
          <h2>Assumptions &amp; limitations</h2>
          <ul className="small">{res.assumptions.map((a: string, i: number) => <li key={i}>{a}</li>)}</ul>
          <p className="small">FX: USD/AED {res.fx.rate} reference ({res.fx.source}). Costs: {res.costs.slippage_bps} bps slippage, {res.costs.fee_per_order_usd} USD/order.</p>
          {res.risk_events.length > 0 && <><h3>Risk overlay events</h3><ul className="small">{res.risk_events.map((e: any, i: number) => <li key={i}>{e.day}: {e.event} at drawdown {pct(e.drawdown)}</li>)}</ul></>}
        </div>
      </div>
      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>Signals vs simulated fills</h2>
        <PriceChart bars={priceBars} signals={markers.s} fills={markers.f} onSelect={(m) => { setHl(String(m.ref)); document.getElementById(`trade-${m.ref}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); }} />
        <p className="small muted">Triangles/diamonds = the decision on the signal day's close. Stars/crosses = simulated fills at the next open (approximation). Click a marker to jump to its trade row.</p>
      </div>
      <div className="card table-wrap" style={{ marginTop: "1rem" }}>
        <h2>Strategy trades (ledger)</h2>
        <table className="small">
          <thead><tr><th>Fill day</th><th>Side</th><th>Qty</th><th>Fill price</th><th>Open</th><th>Notional</th><th>Residual cash</th><th>Signal</th><th>Reason</th></tr></thead>
          <tbody>{P.strategy.trades.map((t: any, i: number) => (
            <tr key={i} id={`trade-${t.decision_ref}`} style={hl === t.decision_ref ? { outline: "2px solid var(--accent)" } : undefined}>
              <td>{t.day}</td><td>{t.side}</td><td className="num">{Number(t.qty).toFixed(6)}</td><td className="num">{Number(t.price).toFixed(2)}</td><td className="num">{Number(t.open).toFixed(2)}</td>
              <td><UsdAed usd={t.notional} /></td><td><UsdAed usd={t.residual_cash} /></td><td>{t.decision_ref?.replace("bt-", "")}</td><td>{t.reason}{t.reduced_for_cash ? " (reduced for cash)" : ""}{t.realized_pnl ? ` · realized ${Number(t.realized_pnl).toFixed(2)} USD` : ""}</td>
            </tr>))}</tbody>
        </table>
      </div>
      <details style={{ marginTop: "1rem" }}>
        <summary>All {res.decisions.length} simulated decisions (table)</summary>
        <div className="table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}>
          <table className="small"><thead><tr><th>Session</th><th>Close</th><th>Avg</th><th>Action</th><th>Reason</th><th>Execution</th></tr></thead>
            <tbody>{res.decisions.slice().reverse().map((d: any) => <tr key={d.ref}><td>{d.session}</td><td className="num">{d.close}</td><td className="num">{d.sma ? Number(d.sma).toFixed(2) : "—"}</td><td>{d.action}</td><td>{d.reason}</td><td>{d.execution ? `${d.execution.status}${d.execution.reason ? ": " + d.execution.reason : ""}` : ""}</td></tr>)}</tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
