import { useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../api";
import { Badge, Confirm, DubaiTime, ErrorNote, Loading, Money, usePoll } from "../components";

function ConnectionTest() {
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="card">
      <h2>Connection test (read-only)</h2>
      <p className="small">Reads account status, cash, positions, open orders, SPY eligibility, the market clock and recent daily bars. It never places an order.</p>
      <button className="primary" disabled={busy} onClick={async () => { setBusy(true); setErr(null); try { setRes(await post("/api/ops/connection-test")); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } }}>
        {busy ? "Testing…" : "Run connection test"}
      </button>
      <ErrorNote error={err} />
      {res && (
        <div style={{ marginTop: ".75rem" }}>
          <p><Badge kind={res.ok ? "ok" : "bad"} icon={res.ok ? "✔" : "✖"}>{res.ok ? "All checks passed" : "Some checks failed"}</Badge> Orders placed: {res.orders_placed ?? 0}</p>
          <table className="small"><tbody>{res.results.map((r: any) => (
            <tr key={r.check}><th scope="row" className={r.ok ? "check-ok" : "check-bad"}>{r.check}</th>
              <td>{typeof r.detail === "string" ? r.detail : <ResultDetail d={r.detail} />}</td></tr>))}</tbody></table>
        </div>
      )}
    </div>
  );
}

function ResultDetail({ d }: { d: any }) {
  if (Array.isArray(d)) return <span>{d.length} item(s): {d.map((x: any) => x.symbol || x.client_order_id).join(", ")}</span>;
  return (
    <span>
      {Object.entries(d).map(([k, v]: any) => (
        <span key={k} style={{ display: "inline-block", marginRight: ".8rem" }}>
          <span className="muted">{k}:</span> {v && typeof v === "object" && "usd" in v ? <Money v={v} /> : v && typeof v === "object" && "dubai" in v ? <DubaiTime t={v} /> : String(v)}
        </span>
      ))}
    </span>
  );
}

export function Setup() {
  const ready = usePoll<any>("/api/controls/readiness", 15000);
  const ov = usePoll<any>("/api/overview", 15000);
  const [confirm, setConfirm] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <div>
      <h1>Setup</h1>
      <div className="grid g2">
        <div className="card">
          <h2>1. What paper trading means</h2>
          <p>Orbit sends orders only to an <strong>Alpaca paper account</strong>: simulated money at the broker. There is no live-money route in this version; a live URL makes the app refuse to start.</p>
          <p className="small">Paper fills are simulated (no market impact, random partial fills ~10% of the time, no dividends). Paper access does not mean you are eligible for live trading. The paper balance (often USD 100,000 by default) is not a realistic budget; Orbit caps each entry at USD 250 (AED 918.13).</p>
        </div>
        <div className="card">
          <h2>2. Connect your paper account</h2>
          <ol className="small">
            <li>In the Alpaca dashboard, switch to your <strong>Paper</strong> account and generate API keys. Use a dedicated paper account with no other holdings or orders.</li>
            <li>Copy <code>.env.example</code> to <code>.env</code> in the project folder and paste the two keys there. Never paste keys into chat, screenshots or the browser.</li>
            <li>Leave <code>APCA_API_BASE_URL=https://paper-api.alpaca.markets</code>.</li>
            <li>Restart Orbit (<code>.\scripts\stop.ps1</code> then <code>.\scripts\start.ps1</code>), then run the connection test below.</li>
          </ol>
          <p className="small">Credentials: {ov.data?.broker_configured ? <Badge kind="ok" icon="✔">found in .env</Badge> : <Badge kind="warn" icon="!">not found</Badge>}</p>
        </div>
        <div className="card">
          <h2>3. Market-data coverage</h2>
          <p className="small">Free Alpaca accounts get the <strong>IEX</strong> feed: real exchange data from one venue, available back to 2016. IEX daily closes can differ slightly from the official consolidated close. The connection test shows the actual range you receive. If history is missing, use a documented CSV import; Orbit never invents older data.</p>
        </div>
        <div className="card">
          <h2>4. The daily schedule</h2>
          <p className="small">The worker evaluates 15 minutes after each NYSE close (times come from the exchange calendar, including holidays, early closes and daylight-saving shifts). If it decides to trade, it submits a DAY market order in the first 5 minutes after the next open, after the broker confirms the market is open.</p>
          {ov.data?.market?.next && <p className="small">Next open: {new Date(ov.data.market.next.open_utc).toLocaleString("en-GB", { timeZone: "Asia/Dubai", weekday: "long", hour: "2-digit", minute: "2-digit" })} Dubai.</p>}
          <p className="small"><strong>Your laptop must be awake and online</strong> at those times. If it sleeps through the window, Orbit records MISSED_EXECUTION and asks you to review — it never sends a late catch-up order. A stopped laptop cannot send its own outage alert.</p>
        </div>
      </div>
      <div style={{ marginTop: "1rem" }}><ConnectionTest /></div>
      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>5. Checks before enabling paper automation</h2>
        {!ready.data ? <Loading /> : (
          <ul>{ready.data.checks.map((c: any) => <li key={c.id} className={c.ok ? "check-ok" : "check-bad"}>{c.text}</li>)}</ul>
        )}
        <p className="small">Recommended: watch at least one observed decision on the <Link to="/decisions">Decisions</Link> page and read its explanation first.</p>
        {msg && <div className="banner info"><div>{msg}</div></div>}
        <div className="row">
          {ov.data?.mode === "PAPER_AUTO" ? <Badge kind="warn" icon="▶">Paper automation is ON</Badge> : (
            <button className="primary" disabled={!ready.data?.ready} onClick={() => setConfirm(true)}>Enable paper automation…</button>
          )}
          <span className="small muted">Current mode: {ov.data?.mode}</span>
        </div>
      </div>
      {confirm && (
        <Confirm title="Enable automatic PAPER orders" needPassword confirmLabel="Enable paper automation" onClose={() => setConfirm(false)}
          onConfirm={async (pw) => { const r: any = await post("/api/controls/activate", { password: pw }); setMsg(r.created_intent ? `Enabled. Entry intent #${r.created_intent} created for the next eligible open.` : r.message); ready.reload(); ov.reload(); }}>
          <p>From the next eligible session open, the worker may submit DAY market orders to your Alpaca <strong>paper</strong> account following the experimental rule. You can return to OBSERVE or pause at any time from Orders &amp; positions.</p>
          <p className="small">If you are flat and the latest completed close is above its 200-session average, an entry intent is created now for the next eligible open.</p>
        </Confirm>
      )}
    </div>
  );
}

export function Operations() {
  const { data: o, error, reload } = usePoll<any>("/api/ops", 30000);
  const rec = usePoll<any>("/api/reconciliation", 30000);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorNote error={error} />;
  if (!o) return <Loading />;
  const ns = o.notifications.settings;
  const save = async (v: any) => { setErr(null); try { await post("/api/ops/notify-settings", v); reload(); } catch (e: any) { setErr(e.message); } };
  return (
    <div>
      <h1>Operations</h1>
      {msg && <div className="banner info" role="status"><div>{msg}</div></div>}
      <ErrorNote error={err} />
      <div className="grid g2">
        <ConnectionTest />
        <div className="card">
          <h2>Worker &amp; schedule</h2>
          <p><Badge kind={o.worker.state === "RUNNING" ? "ok" : "bad"}>{o.worker.state}</Badge> {o.worker.text}</p>
          <p className="small">Last successful evaluation: {o.last_successful_evaluation ? <>{o.last_successful_evaluation.key.split(":")[1]} at <DubaiTime t={o.last_successful_evaluation.at} /></> : "none yet"}</p>
          <p className="small">External heartbeat: {o.heartbeat.configured ? (
            o.heartbeat.last_error ? <Badge kind="bad" icon="✖">last ping failed ({o.heartbeat.last_error})</Badge>
              : o.heartbeat.last_ok ? <Badge kind="ok" icon="✔">last ping {new Date(o.heartbeat.last_ok).toLocaleTimeString("en-GB", { timeZone: "Asia/Dubai" })} Dubai</Badge>
              : <Badge kind="warn">configured, no ping yet</Badge>
          ) : <Badge kind="warn" icon="!">not configured</Badge>} <span className="muted">{o.heartbeat.meaning}</span></p>
          <h3>Monitoring routine (Dubai time, from the exchange calendar)</h3>
          <table className="small"><thead><tr><th>Session</th><th>Check orders at open</th><th>Window ends</th><th>Evening decision</th></tr></thead>
            <tbody>{o.monitoring_routine.map((m: any) => <tr key={m.session}><td>{m.session}{m.early_close && " (early close)"}</td><td>{m.check_open_dubai}</td><td>{m.window_end_dubai}</td><td>{m.evaluation_dubai}</td></tr>)}</tbody></table>
          <ul className="small">
            <li>Morning: read the previous evening's decision and its explanation.</li>
            <li>If an intent is pending: be online at the open and check the order resolves (filled/cancelled) — supervise the first automated sessions closely.</li>
            <li>Weekly: review executions and the reconciliation report. Monthly: review research experiments (without re-tuning on the holdout).</li>
          </ul>
        </div>
      </div>
      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>Reconciliation report</h2>
        {rec.data?.report ? (
          <>
            <p><Badge kind={rec.data.report.ok ? "ok" : "bad"} icon={rec.data.report.ok ? "✔" : "✖"}>{rec.data.report.ok ? "Clean" : "Discrepancies"}</Badge> Checked <DubaiTime t={rec.data.report.at} /></p>
            {rec.data.report.discrepancies.map((d: any, i: number) => <div key={i} className="banner bad"><div><strong>{d.kind}</strong>: {d.detail}</div></div>)}
            <div className="grid g2">
              <div><h3>Broker snapshot</h3><pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(rec.data.report.broker, null, 2)}</pre></div>
              <div><h3>Orbit's local view</h3><pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(rec.data.report.local, null, 2)}</pre></div>
            </div>
          </>
        ) : <p className="muted">No reconciliation yet (needs paper credentials and a running worker).</p>}
        {rec.data && <p className="small">From the broker: {rec.data.field_sources.from_broker.join("; ")}. Derived by Orbit: {rec.data.field_sources.derived_by_orbit.join("; ")}.</p>}
      </div>
      <div className="grid g2" style={{ marginTop: "1rem" }}>
        <div className="card">
          <h2>Phone notifications (optional, ntfy)</h2>
          <p className="small">The in-app inbox works without any setup. Push uses ntfy. An unguessable topic is <strong>not</strong> private authentication, so pushes contain only a redacted summary unless you configure an ntfy access token and opt in.</p>
          <p className="small">Topic: {o.config.ntfy_topic} · Token: {o.config.ntfy_token} · Server: {o.config.ntfy_server}</p>
          <label className="row"><input type="checkbox" checked={!!ns.enabled} onChange={(e) => save({ enabled: e.target.checked })} /> Send push notifications</label>
          <label className="row"><input type="checkbox" checked={!!ns.include_financial_details} onChange={(e) => save({ include_financial_details: e.target.checked })} /> Include sizes/prices (requires ntfy token)</label>
          <div className="row">
            <label htmlFor="qs">Quiet hours (Dubai)</label>
            <input id="qs" type="time" value={ns.quiet_start} onChange={(e) => save({ quiet_start: e.target.value })} />–
            <input aria-label="Quiet hours end" type="time" value={ns.quiet_end} onChange={(e) => save({ quiet_end: e.target.value })} />
          </div>
          <p className="small muted">Critical alerts (missed execution, rejection, security pause, stale worker cycle errors) bypass quiet hours. Routine HOLD/SKIP results are grouped in one daily summary.</p>
          <button onClick={async () => { setErr(null); try { const r: any = await post("/api/ops/notify-test"); setMsg(r.message); reload(); } catch (e: any) { setErr(e.message); } }}>Send test notification</button>
          <h3>Outbox</h3>
          <p className="small muted">{o.notifications.meaning}</p>
          <table className="small"><thead><tr><th>Created</th><th>Status</th><th>Title</th><th>Tries</th><th>Error</th></tr></thead>
            <tbody>{o.notifications.outbox.slice(0, 10).map((n: any) => <tr key={n.id}><td><DubaiTime t={n.created_at} /></td><td>{n.status}</td><td>{n.title}</td><td>{n.attempts}</td><td>{n.last_error}</td></tr>)}</tbody></table>
        </div>
        <div className="card">
          <h2>Configuration (masked)</h2>
          <table className="small"><tbody>{Object.entries(o.config).map(([k, v]: any) => <tr key={k}><th scope="row">{k}</th><td className="mono">{String(v)}</td></tr>)}</tbody></table>
          <h3>Backups</h3>
          <button onClick={async () => { setErr(null); try { const r: any = await post("/api/ops/backup"); setMsg(`Backup ${r.file}: integrity ${r.integrity}`); reload(); } catch (e: any) { setErr(e.message); } }}>Back up database now</button>
          <ul className="small">{o.backups.map((b: any) => <li key={b.name}>{b.name} ({Math.round(b.size / 1024)} KB)</li>)}</ul>
          <p className="small muted">Restore drill: <code>.\scripts\restore-drill.ps1</code>. Restore: stop Orbit, then <code>.\scripts\restore.ps1 &lt;file&gt;</code>.</p>
          <h3>Exports</h3>
          <a className="btn" href="/api/export/decisions">Decisions &amp; intents (JSON, redacted)</a>
          <h3>Recent jobs</h3>
          <table className="small"><tbody>{o.jobs.slice(0, 8).map((j: any, i: number) => <tr key={i}><td>{j.type}</td><td>{j.status}</td><td className="mono">{j.key.split(":").slice(1).join(":")}</td><td>{j.error}</td></tr>)}</tbody></table>
        </div>
      </div>
    </div>
  );
}

export function Security() {
  const { data: s, error, reload } = usePoll<any>("/api/security", 30000);
  const ov = usePoll<any>("/api/overview", 15000);
  const [modal, setModal] = useState<string | null>(null);
  if (error) return <ErrorNote error={error} />;
  if (!s) return <Loading />;
  const f = ov.data?.flags;
  return (
    <div>
      <h1>Security</h1>
      <div className="banner warn"><span aria-hidden="true">!</span><div>{s.disclaimer}</div></div>
      <div className="grid g2">
        <div className="card">
          <h2>Status</h2>
          <table className="small"><tbody>
            <tr><th scope="row">Mode</th><td>{s.mode}</td></tr>
            <tr><th scope="row">Security pause</th><td>{s.security_paused ? <Badge kind="bad" icon="⛔">ACTIVE — {s.security_pause_reason}</Badge> : <Badge kind="ok" icon="✔">not active</Badge>}</td></tr>
            <tr><th scope="row">Access</th><td>{s.access.auth}; bound to {s.access.bind} ({s.access.deployment_mode}); sessions expire after {s.access.session_hours} h</td></tr>
            <tr><th scope="row">Failed logins (24 h)</th><td>{s.access.failed_logins_24h} (throttled; never pauses trading)</td></tr>
            <tr><th scope="row">Last backup</th><td>{s.last_backup || "none yet"}</td></tr>
            <tr><th scope="row">Worker</th><td>{s.worker.state}</td></tr>
          </tbody></table>
          <div className="row" style={{ marginTop: ".75rem" }}>
            {!s.security_paused ? <button className="danger" onClick={() => setModal("pause")}>Security pause (incident)</button> : <button onClick={() => setModal("resume")}>Resume after review…</button>}
            {f?.risk_paused_entries && <button onClick={() => setModal("risk")}>Resume entries after drawdown review…</button>}
            {f && !f.performance_valid && <button onClick={() => setModal("cash")}>Mark cash activity reviewed…</button>}
          </div>
        </div>
        <div className="card">
          <h2>Incident response</h2>
          <ol className="small">{s.incident_checklist.map((x: string, i: number) => <li key={i}>{x}</li>)}</ol>
        </div>
      </div>
      <div className="grid g2" style={{ marginTop: "1rem" }}>
        <div className="card"><h2>Not implemented / limitations</h2><ul className="small">{s.not_implemented.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></div>
        <div className="card"><h2>Recent warning/critical audit events</h2><ul className="small">{s.recent_security_events.map((e: any, i: number) => <li key={i}><DubaiTime t={e.at} /> {e.action} ({e.severity})</li>)}</ul></div>
      </div>
      <details style={{ marginTop: "1rem" }}><summary>Verification results (docs/TEST_EVIDENCE.md)</summary><pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>{s.verification}</pre></details>
      {modal === "pause" && <Confirm title="Security pause" danger needNote="What happened? (kept in the audit log)" onClose={() => setModal(null)}
        onConfirm={async (_pw, note) => { await post("/api/controls/security-pause", { note }); reload(); ov.reload(); }}>
        <p>Blocks all automated order submissions (buys and sells). Read-only reconciliation continues. Orders already at the broker are not cancelled by this switch.</p></Confirm>}
      {modal === "resume" && <Confirm title="Resume after security review" needPassword needNote="What did you check? (min 10 characters)" onClose={() => setModal(null)}
        onConfirm={async (pw, note) => { await post("/api/controls/resume-security", { password: pw, note }); reload(); ov.reload(); }}>
        <p>Requires a clean reconciliation from the last 5 minutes. If keys may be exposed, rotate them first.</p></Confirm>}
      {modal === "risk" && <RiskResume onClose={() => { setModal(null); ov.reload(); }} />}
      {modal === "cash" && <Confirm title="Cash activity reviewed" needPassword needNote="Describe the deposit/withdrawal you reconciled" onClose={() => setModal(null)}
        onConfirm={async (pw, note) => { await post("/api/controls/cash-reviewed", { password: pw, note }); ov.reload(); }}>
        <p>Performance stays labelled as affected by external cash flows in history. The high-water mark is not silently reset.</p></Confirm>}
    </div>
  );
}

function RiskResume({ onClose }: { onClose: () => void }) {
  const [override, setOverride] = useState(false);
  return (
    <Confirm title="Resume entries after drawdown review" needPassword needNote="Your review reason (min 10 characters)" onClose={onClose}
      onConfirm={async (pw, note) => { await post("/api/controls/resume-risk", { password: pw, note, override }); }}>
      <p>If the drawdown is still at or above the blocking threshold, resuming requires an explicit, versioned risk-policy override with your reason.</p>
      <label className="row"><input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} /> Record a versioned override</label>
    </Confirm>
  );
}
