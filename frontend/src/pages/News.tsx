import { useState } from "react";
import { post } from "../api";
import { Badge, DubaiTime, ErrorNote, Loading, pct, usePoll } from "../components";

function ago(iso: string) {
  const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} h ago` : new Date(iso).toLocaleDateString("en-GB", { timeZone: "Asia/Dubai", day: "2-digit", month: "short" });
}

export function News() {
  const news = usePoll<any>("/api/news", 120000);
  const fc = usePoll<any>("/api/forecasts", 60000);
  return (
    <div>
      <h1>News &amp; forecasts</h1>
      <p className="lede">Read the headlines, make your own UP/DOWN call for the next session before the market opens, and let Orbit keep an honest score.
        This page is for learning only: neither news nor forecasts can place or change orders.</p>
      <div className="grid news-grid">
        <div className="card">
          <h2>Latest SPY &amp; market headlines</h2>
          {news.error && <ErrorNote error={news.error} />}
          {!news.data ? <Loading /> : !news.data.available ? (
            <div className="banner info"><span aria-hidden="true">ℹ</span><div>{news.data.reason}</div></div>
          ) : (
            <>
              <ul className="news-list">
                {news.data.items.map((n: any) => (
                  <li key={n.id}>
                    <div className="news-meta small muted">
                      <span>{n.created_at ? ago(n.created_at) : ""}</span>
                      <span>· {n.source}</span>
                      {n.symbols?.length > 0 && <span className="news-syms">{n.symbols.slice(0, 4).join(" ")}</span>}
                    </div>
                    {n.url ? <a href={n.url} target="_blank" rel="noopener noreferrer" className="news-head">{n.headline}</a>
                      : <span className="news-head">{n.headline}</span>}
                    {n.summary && <p className="small muted news-sum">{n.summary}</p>}
                  </li>
                ))}
              </ul>
              <p className="small muted">{news.data.note}</p>
            </>
          )}
        </div>
        <div>
          {fc.error && <ErrorNote error={fc.error} />}
          {!fc.data ? <Loading /> : <ForecastPanel d={fc.data} reload={fc.reload} />}
        </div>
      </div>
    </div>
  );
}

function ForecastPanel({ d, reload }: { d: any; reload: () => void }) {
  const [dir, setDir] = useState<"UP" | "DOWN" | null>(null);
  const [conf, setConf] = useState(60);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const made = d.next.already_made;
  const sc = d.score;
  return (
    <>
      <div className="card">
        <h2>Your call for {d.next.target_session}</h2>
        {made ? (
          <p>
            Recorded: <Badge kind={made.direction === "UP" ? "ok" : "bad"} icon={made.direction === "UP" ? "▲" : "▼"}>{made.direction}</Badge>{" "}
            at {made.confidence}% confidence. It will be scored after that session closes.
            {made.reason && <span className="muted small"> Your reason: “{made.reason}”</span>}
          </p>
        ) : (
          <>
            <p className="small muted">Will SPY close <strong>higher or lower</strong> on {d.next.target_session} than it did on {d.next.base_session}?
              Deadline: <DubaiTime t={d.next.deadline} /> (the market open). One call per session; it can't be changed afterwards.</p>
            <div className="row" style={{ marginBottom: ".7rem" }}>
              <button className={`call up ${dir === "UP" ? "on" : ""}`} aria-pressed={dir === "UP"} onClick={() => setDir("UP")}>▲ UP</button>
              <button className={`call down ${dir === "DOWN" ? "on" : ""}`} aria-pressed={dir === "DOWN"} onClick={() => setDir("DOWN")}>▼ DOWN</button>
            </div>
            <div className="field">
              <label htmlFor="conf">How sure are you? {conf}%</label>
              <input id="conf" type="range" min={50} max={100} step={5} value={conf} onChange={(e) => setConf(Number(e.target.value))} style={{ width: "100%" }} />
            </div>
            <div className="field">
              <label htmlFor="why">Why? (which headline or reason, optional)</label>
              <textarea id="why" rows={3} maxLength={2000} style={{ width: "100%" }} value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
            <ErrorNote error={err} />
            <button className="primary" disabled={!dir || busy} onClick={async () => {
              setBusy(true); setErr(null);
              try { await post("/api/forecasts", { direction: dir, confidence: conf, reason }); setDir(null); setReason(""); reload(); }
              catch (e: any) { setErr(e.message); } finally { setBusy(false); }
            }}>Record my call</button>
          </>
        )}
      </div>

      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>Scorecard</h2>
        <div className="grid g3">
          <div><div className="kpi-label">Your hit rate</div><div className="kpi-value">{sc.accuracy == null ? "—" : pct(sc.accuracy, 0)}</div>
            <div className="small muted">{sc.hits} of {sc.scored} scored</div></div>
          <div><div className="kpi-label">"Always UP" would score</div><div className="kpi-value">{sc.always_up_accuracy == null ? "—" : pct(sc.always_up_accuracy, 0)}</div>
            <div className="small muted">same sessions</div></div>
          <div><div className="kpi-label">Waiting to score</div><div className="kpi-value">{sc.pending}</div>
            <div className="small muted">{sc.flat ? `${sc.flat} flat (not counted)` : " "}</div></div>
        </div>
        <div className="banner info" style={{ marginTop: ".9rem", marginBottom: 0 }}><span aria-hidden="true">ℹ</span><div>
          {sc.verdict}
          {sc.p_value_vs_coin != null && sc.scored >= 20 && <div className="small muted">Chance of doing at least this well by coin-flipping: {pct(sc.p_value_vs_coin, 1)}.</div>}
        </div></div>
      </div>

      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>History</h2>
        {d.items.length === 0 ? <p className="muted">No calls yet. Make your first one above.</p> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Session</th><th>Your call</th><th>Sure</th><th>Result</th><th>Closes (USD)</th></tr></thead>
            <tbody>{d.items.map((f: any) => (
              <tr key={f.id} title={f.reason || undefined}>
                <td>{f.target_session}</td>
                <td className={f.direction === "UP" ? "chg up" : "chg down"}>{f.direction === "UP" ? "▲" : "▼"} {f.direction}</td>
                <td className="num">{f.confidence}%</td>
                <td>{f.outcome == null ? <Badge kind="info" icon="…">pending</Badge> : f.correct == null ? <Badge kind="">FLAT</Badge>
                  : f.correct ? <Badge kind="ok" icon="✔">right ({f.outcome})</Badge> : <Badge kind="bad" icon="✖">wrong ({f.outcome})</Badge>}</td>
                <td className="num small">{f.base_close ? `${Number(f.base_close).toFixed(2)} → ${Number(f.outcome_close).toFixed(2)}` : "—"}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
        <details style={{ marginTop: ".8rem", marginBottom: 0 }}><summary>The rules</summary>
          <ul className="small">{d.rules.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul></details>
      </div>
    </>
  );
}
