import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { post } from "../api";
import { Badge, Confirm, ErrorNote, Loading, Money, pct, SampleBanner, usePoll } from "../components";
import { CandleChart, ChartMarker, dayToTs } from "../candles";
import { LivePanel } from "../live";

export function Market() {
  const ds = usePoll<any>("/api/datasets", 0);
  const [source, setSource] = useState("auto");
  const chart = usePoll<any>(`/api/market/chart?source=${source}`, 60000);
  const strat = usePoll<any>("/api/strategy", 0);
  const nav = useNavigate();
  const [edit, setEdit] = useState(false);
  const c = chart.data;
  const storedCandles = (c?.bars || []).map((b: any) => ({ t: dayToTs(b.day), o: Number(b.open), h: Number(b.high), l: Number(b.low), c: Number(b.close) }));
  const storedSma = (c?.bars || []).filter((b: any) => b.sma200).map((b: any) => ({ t: dayToTs(b.day), value: Number(b.sma200) }));
  const storedMarks: ChartMarker[] = (c?.decisions || []).filter((d: any) => d.action === "BUY" || d.action === "SELL")
    .map((d: any) => ({ t: dayToTs(d.session), side: d.action, kind: "decision", text: `${d.action} #${d.id}` }));
  const decByDay = new Map<string, any>((c?.decisions || []).map((d: any) => [d.session, d]));
  return (
    <div>
      <h1>Market &amp; strategy</h1>
      <p className="lede">Live SPY prices from your Alpaca paper data feed, with Orbit's 200-session average and its own signals on top.</p>
      <LivePanel stored={chart.data?.dataset?.mode === "ALPACA" ? c : null} />
      <h2 className="section">Stored daily history <span className="muted small">(the exact data decisions use)</span></h2>
      <div className="row" style={{ marginBottom: ".75rem" }}>
        <label htmlFor="src" className="sr-only">Data source</label>
        <select id="src" value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="auto">Latest (Alpaca if available)</option>
          {ds.data?.items.map((d: any) => <option key={d.id} value={`dataset:${d.id}`}>#{d.id} {d.mode} · {d.describe.first_session} → {d.describe.last_session}</option>)}
        </select>
        {c?.available && <Badge kind={c.dataset.mode === "SAMPLE" ? "sample" : "accent"}>{c.dataset.mode}</Badge>}
      </div>
      {chart.error && <ErrorNote error={chart.error} />}
      {c && !c.available && <div className="banner info"><span aria-hidden="true">ℹ</span><div>{c.reason}</div></div>}
      {c?.available && (
        <>
          {c.dataset.mode === "SAMPLE" && <SampleBanner />}
          <div className="card">
            <h2>SPY daily candles vs 200-session average</h2>
            <p className="small muted">Source: {c.dataset.source}. Basis: {c.dataset.price_basis}. Dividends {c.dataset.dividends_included ? "included" : "excluded (price-only)"}.
              Range {c.dataset.first_session} → {c.dataset.last_session} ({c.dataset.bar_count} sessions{c.missing_sessions.length ? `, ${c.missing_sessions.length} missing shown as gaps` : ""}).</p>
            <CandleChart candles={storedCandles} sma={storedSma} markers={storedMarks} intraday={false} height={360}
              resetKey={`stored-${c.dataset.id}`} ariaLabel="Stored SPY daily candles with the 200-session average. A table alternative follows." />
            {c.dataset.mode !== "ALPACA" && <p className="small muted">Signal and fill markers for this dataset are shown on the Research page (they belong to a backtest ledger, not the paper account).</p>}
          </div>
          <details>
            <summary>Table alternative (last 30 sessions)</summary>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Session</th><th>Close (USD)</th><th>200-avg (USD)</th><th>Close vs avg</th><th>Decision</th></tr></thead>
                <tbody>
                  {c.bars.slice(-30).reverse().map((b: any) => {
                    const d = decByDay.get(b.day);
                    return (
                      <tr key={b.day} className={d ? "clickable" : ""} onClick={() => d && nav(`/decisions/${d.id}`)}>
                        <td>{b.day}</td><td className="num">{Number(b.close).toFixed(2)}</td><td className="num">{b.sma200 ? Number(b.sma200).toFixed(2) : "warm-up"}</td>
                        <td>{b.sma200 ? (Number(b.close) > Number(b.sma200) ? "above ▲" : Number(b.close) < Number(b.sma200) ? "below ▼" : "equal =") : "—"}</td>
                        <td>{d ? `${d.action} (#${d.id})` : ""}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
      {strat.data && (
        <div className="grid g2" style={{ marginTop: "1rem" }}>
          <div className="card">
            <h2>The rule in plain English</h2>
            <Badge kind="warn" icon="⚠">{strat.data.label}</Badge>
            <ol>{strat.data.rule_plain_english.map((r: string, i: number) => <li key={i}>{r}</li>)}</ol>
          </div>
          <div className="card">
            <h2>Parameters (version {strat.data.active_version.id})</h2>
            <table>
              <tbody>
                <tr><th scope="row">Average window</th><td>{strat.data.params.sma_window} sessions</td></tr>
                <tr><th scope="row">Completion buffer</th><td>{strat.data.params.completion_buffer_minutes} min after close</td></tr>
                <tr><th scope="row">Entry fraction</th><td>{pct(strat.data.params.entry_fraction, 0)} of equity</td></tr>
                <tr><th scope="row">Hard entry cap</th><td><Money v={strat.data.params_money.max_entry_notional} /></td></tr>
                <tr><th scope="row">Cash reserve</th><td><Money v={strat.data.params_money.cash_reserve} /></td></tr>
                <tr><th scope="row">Minimum order</th><td><Money v={strat.data.params_money.min_order} /></td></tr>
                <tr><th scope="row">Submission window</th><td>first {strat.data.params.execution_window_minutes} min after open</td></tr>
                <tr><th scope="row">Risk review</th><td>warn {pct(strat.data.risk_policy.warn_fraction, 0)}, pause entries {pct(strat.data.risk_policy.pause_fraction, 0)}</td></tr>
              </tbody>
            </table>
            <button style={{ marginTop: ".75rem" }} onClick={() => setEdit(true)}>Change settings (creates a new version)</button>
            <details style={{ marginTop: ".75rem" }}>
              <summary>Version history</summary>
              <ul>{strat.data.versions.map((v: any) => <li key={v.id}>v{v.id} {v.active && <Badge kind="ok">active</Badge>} — {v.reason} <span className="muted small">({new Date(v.created_at.utc).toLocaleString()})</span></li>)}</ul>
            </details>
          </div>
        </div>
      )}
      {edit && strat.data && <SettingsEditor current={strat.data.params} onClose={() => { setEdit(false); strat.reload(); }} />}
    </div>
  );
}

function SettingsEditor({ current, onClose }: { current: any; onClose: () => void }) {
  const [vals, setVals] = useState<any>({ entry_fraction: current.entry_fraction, max_entry_notional_usd: current.max_entry_notional_usd, cash_reserve_usd: current.cash_reserve_usd });
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const fields: [string, string][] = [["entry_fraction", "Entry fraction (0.01–1)"], ["max_entry_notional_usd", "Hard entry cap (USD)"], ["cash_reserve_usd", "Cash reserve (USD)"]];
  return (
    <div className="modal-back" role="dialog" aria-modal="true" aria-label="Change strategy settings">
      <div className="card modal">
        <h2>Change strategy settings</h2>
        <p className="small">Edits create a new version effective for future decisions only. Old decisions and reports are never rewritten. Pending intents from the old version are moved to review instead of executing.</p>
        {fields.map(([k, label]) => (
          <div className="field" key={k}>
            <label htmlFor={k}>{label}</label>
            <input id={k} value={vals[k]} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} inputMode="decimal" />
            {k !== "entry_fraction" && vals[k] && !isNaN(Number(vals[k])) && <span className="small muted"> ≈ AED {(Number(vals[k]) * 3.6725).toFixed(2)}</span>}
          </div>
        ))}
        <div className="field"><label htmlFor="reason">Reason for the change</label><input id="reason" style={{ width: "100%" }} value={reason} onChange={(e) => setReason(e.target.value)} /></div>
        <ErrorNote error={err} />
        {preview && (
          <div className="banner info"><div>
            <strong>Effect on pending intents:</strong>{" "}
            {preview.pending_intents.length ? preview.pending_intents.map((p: any) => <div key={p.id}>#{p.id} {p.kind} {p.side}: {p.effect}</div>) : "none pending."}
          </div></div>
        )}
        <div className="row">
          <button onClick={onClose}>Cancel</button>
          <button onClick={async () => { setErr(null); try { setPreview(await post("/api/strategy/preview", { values: vals })); } catch (e: any) { setErr(e.message); } }}>Preview</button>
          <button className="primary" disabled={!preview || reason.length < 5} onClick={() => setConfirm(true)}>Save new version</button>
        </div>
      </div>
      {confirm && (
        <Confirm title="Save new strategy version" needPassword onClose={() => setConfirm(false)}
          onConfirm={async (pw) => { await post("/api/strategy/update", { values: vals, reason, password: pw }); onClose(); }}>
          <p>This is an audited change to trading settings. The execution boundary still enforces SPY-only, paper-only, no margin and the hard cap.</p>
        </Confirm>
      )}
    </div>
  );
}
