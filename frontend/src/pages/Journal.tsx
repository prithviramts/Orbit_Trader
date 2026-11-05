import { useState } from "react";
import { post } from "../api";
import { DubaiTime, ErrorNote, Loading, Money, usePoll } from "../components";

export function Journal() {
  const j = usePoll<any>("/api/journal", 0);
  const ex = usePoll<any>("/api/expenses", 0);
  const [f, setF] = useState({ title: "", source_url: "", published_at: "", notes: "" });
  const [e, setE] = useState({ day: new Date().toISOString().slice(0, 10), category: "other", description: "", currency: "USD", amount: "" });
  const [err, setErr] = useState<string | null>(null);
  return (
    <div>
      <h1>News journal &amp; project costs</h1>
      <div className="grid g2">
        <div className="card">
          <h2>Add a news/event note</h2>
          <p className="small muted">{j.data?.note} There is no live news feed in v1.</p>
          <div className="field"><label htmlFor="jt">Headline / event</label><input id="jt" style={{ width: "100%" }} value={f.title} maxLength={200} onChange={(x) => setF({ ...f, title: x.target.value })} /></div>
          <div className="field"><label htmlFor="ju">Source URL (reference only)</label><input id="ju" style={{ width: "100%" }} value={f.source_url} onChange={(x) => setF({ ...f, source_url: x.target.value })} /></div>
          <div className="field"><label htmlFor="jp">Published at (your local time)</label><input id="jp" type="datetime-local" value={f.published_at} onChange={(x) => setF({ ...f, published_at: x.target.value })} /></div>
          <div className="field"><label htmlFor="jn">Your notes</label><textarea id="jn" rows={3} style={{ width: "100%" }} value={f.notes} onChange={(x) => setF({ ...f, notes: x.target.value })} /></div>
          <button className="primary" disabled={!f.title} onClick={async () => {
            setErr(null);
            try {
              await post("/api/journal", { title: f.title, source_url: f.source_url, notes: f.notes, instrument: "SPY", published_at: f.published_at ? new Date(f.published_at).toISOString() : null });
              setF({ title: "", source_url: "", published_at: "", notes: "" }); j.reload();
            } catch (x: any) { setErr(x.message); }
          }}>Save note</button>
        </div>
        <div className="card">
          <h2>Add a project expense</h2>
          <p className="small muted">Actual money you spent on this project. Kept separate from simulated trading P&amp;L. Free-first setup is the default.</p>
          <div className="row">
            <div className="field"><label htmlFor="ed">Date</label><input id="ed" type="date" value={e.day} onChange={(x) => setE({ ...e, day: x.target.value })} /></div>
            <div className="field"><label htmlFor="ec">Category</label><select id="ec" value={e.category} onChange={(x) => setE({ ...e, category: x.target.value })}>{["data", "hosting", "ai", "software", "education", "other"].map((c) => <option key={c}>{c}</option>)}</select></div>
            <div className="field"><label htmlFor="ecur">Currency</label><select id="ecur" value={e.currency} onChange={(x) => setE({ ...e, currency: x.target.value })}><option>USD</option><option>AED</option></select></div>
            <div className="field"><label htmlFor="ea">Amount</label><input id="ea" size={8} inputMode="decimal" value={e.amount} onChange={(x) => setE({ ...e, amount: x.target.value })} /></div>
          </div>
          <div className="field"><label htmlFor="edesc">Description</label><input id="edesc" style={{ width: "100%" }} value={e.description} onChange={(x) => setE({ ...e, description: x.target.value })} /></div>
          <button className="primary" disabled={!e.amount} onClick={async () => { setErr(null); try { await post("/api/expenses", e); setE({ ...e, amount: "", description: "" }); ex.reload(); } catch (x: any) { setErr(x.message); } }}>Save expense</button>
        </div>
      </div>
      <ErrorNote error={err} />
      <div className="card table-wrap" style={{ marginTop: "1rem" }}>
        <h2>Journal</h2>
        {!j.data ? <Loading /> : j.data.items.length === 0 ? <p className="muted">No entries.</p> : (
          <table className="small"><thead><tr><th>First seen</th><th>Published</th><th>Event</th><th>Source</th><th>Notes</th><th></th></tr></thead>
            <tbody>{j.data.items.map((x: any) => (
              <tr key={x.id}>
                <td><DubaiTime t={x.first_seen_at} /></td><td><DubaiTime t={x.published_at} /></td><td>{x.title}</td>
                <td>{x.source_url ? <a href={x.source_url} target="_blank" rel="noopener noreferrer nofollow">{(() => { try { return new URL(x.source_url).hostname; } catch { return "link"; } })()} ↗</a> : "—"}</td>
                <td>{x.notes}</td>
                <td><button onClick={async () => { await post(`/api/journal/${x.id}/delete`); j.reload(); }}>Delete</button></td>
              </tr>))}</tbody></table>
        )}
      </div>
      <div className="card table-wrap" style={{ marginTop: "1rem" }}>
        <h2>Project costs</h2>
        {!ex.data ? <Loading /> : (
          <>
            <table className="small"><thead><tr><th>Date</th><th>Category</th><th>Original</th><th>FX used</th><th>Amount</th><th>Description</th></tr></thead>
              <tbody>{ex.data.items.map((x: any) => <tr key={x.id}><td>{x.day}</td><td>{x.category}</td><td>{x.currency} {x.amount}</td><td>{x.fx_rate}</td><td><Money v={x.amount_money} /></td><td>{x.description}</td></tr>)}</tbody></table>
            <p>Total actual costs: <Money v={ex.data.total} /></p>
            <p>Paper P&amp;L since activation: <Money v={ex.data.paper_pnl_since_activation} sign /> · Hypothetical net after costs: <Money v={ex.data.hypothetical_net} sign /></p>
            <p className="small muted">{ex.data.note}</p>
            <details><summary>Optional future expenses (no prices invented; check providers yourself; nothing is purchased automatically)</summary><ul className="small">{ex.data.optional_costs.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul></details>
          </>
        )}
      </div>
    </div>
  );
}
