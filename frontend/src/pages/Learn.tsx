import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../api";
import { Badge, ErrorNote, Loading, SampleBanner, usePoll } from "../components";

export function Learn() {
  const { data, reload } = usePoll<any>("/api/learn", 0);
  const runs = usePoll<any>("/api/backtests", 0);
  const weekly = usePoll<any>("/api/learn/weekly", 0);
  if (!data) return <Loading />;
  const done = (id: string) => data.progress?.[`lesson:${id}`]?.correct;
  const completed = data.lessons.filter((l: any) => done(l.id)).length;
  return (
    <div>
      <h1>Learn</h1>
      <p className="muted">Five short lessons, a predict-then-reveal replay, and a weekly review. Progress is saved in the local database.</p>
      <div className="row" style={{ marginBottom: "1rem" }}>
        <Badge kind={completed === data.lessons.length ? "ok" : "info"} icon="✎">{completed}/{data.lessons.length} lessons complete</Badge>
        <Badge kind="" icon="🔒" title="Model controls are a later phase and unlock only after the probability lesson.">
          Future AI/model controls: {done("l5") ? "lesson prerequisite met (not built in v1)" : "locked until lesson 5"}
        </Badge>
      </div>
      <div className="grid g2">
        {data.lessons.map((l: any, i: number) => <Lesson key={l.id} n={i + 1} l={l} sources={data.sources} saved={data.progress?.[`lesson:${l.id}`]} onSaved={reload} />)}
      </div>
      <div className="card" style={{ marginTop: "1rem" }}>
        <h2>Weekly review</h2>
        {weekly.data ? (
          <p>{weekly.data.prompt} {weekly.data.decision_id && <Link to={`/decisions/${weekly.data.decision_id}`}>Open decision {weekly.data.session} →</Link>}</p>
        ) : <Loading />}
      </div>
      <Replay runs={runs.data?.items?.filter((r: any) => r.status === "DONE") || []} />
      <details style={{ marginTop: "1rem" }} open>
        <summary>Glossary</summary>
        <dl>
          {data.glossary.map((g: any) => (
            <div key={g.id} style={{ marginBottom: ".6rem" }}>
              <dt><strong>{g.term}</strong></dt>
              <dd style={{ margin: 0 }}>{g.text} {g.source && <a href={g.source} target="_blank" rel="noopener noreferrer">Official source ↗</a>}</dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}

function Lesson({ n, l, sources, saved, onSaved }: { n: number; l: any; sources: any; saved: any; onSaved: () => void }) {
  const [pick, setPick] = useState<number | null>(saved ? saved.picked : null);
  return (
    <div className="card">
      <h2>{n}. {l.title} {saved?.correct && <Badge kind="ok" icon="✔">done</Badge>}</h2>
      <ul>{l.body.map((b: string, i: number) => <li key={i}>{b}</li>)}</ul>
      <p><strong>Check:</strong> {l.check.q}</p>
      {l.check.options.map((o: string, i: number) => (
        <button key={i} className={`lesson-opt ${pick === i ? (i === l.check.answer ? "right" : "wrong") : ""}`}
          onClick={async () => { setPick(i); await post("/api/learn/progress", { key: `lesson:${l.id}`, value: { picked: i, correct: i === l.check.answer } }); onSaved(); }}>
          {o} {pick === i && (i === l.check.answer ? " ✔ correct" : " ✖ not quite")}
        </button>
      ))}
      {pick !== null && <p className="small">{l.check.why}</p>}
      {l.source && <a className="small" href={sources[l.source]} target="_blank" rel="noopener noreferrer">Read the official source ↗</a>}
    </div>
  );
}

function Replay({ runs }: { runs: any[] }) {
  const [rid, setRid] = useState<number | null>(null);
  const [rep, setRep] = useState<any>(null);
  const [i, setI] = useState(0);
  const [guess, setGuess] = useState<string | null>(null);
  const [score, setScore] = useState({ right: 0, total: 0 });
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (!rid && runs.length) setRid(runs[0].id); }, [runs]);
  useEffect(() => {
    if (!rid) return;
    get(`/api/learn/replay/${rid}`).then((r) => { setRep(r); setI(0); setGuess(null); }).catch((e) => setErr(e.message));
  }, [rid]);
  const step = rep?.steps?.[i];
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Predict the rule's next action</h2>
      {!runs.length ? <p className="muted">Run a backtest on the Research page first (SAMPLE is fine), then come back.</p> : (
        <>
          <div className="row">
            <label htmlFor="rr">Replay from run</label>
            <select id="rr" value={rid ?? ""} onChange={(e) => setRid(Number(e.target.value))}>{runs.map((r) => <option key={r.id} value={r.id}>#{r.id} {r.data_mode} {r.period}</option>)}</select>
            <span className="small muted">Score: {score.right}/{score.total}</span>
          </div>
          <ErrorNote error={err} />
          {rep && rep.data_mode === "SAMPLE" && <SampleBanner />}
          {step && (
            <div style={{ marginTop: ".75rem" }}>
              <p><strong>Session {step.session}</strong> (step {i + 1} of {rep.steps.length}). Close <span className="num">{step.close}</span> USD; 200-session average <span className="num">{step.sma ? Number(step.sma).toFixed(2) : "N/A"}</span> USD.
                Previous target {step.previous_target}; strategy holds {Number(step.held_qty).toFixed(4)} SPY.</p>
              <p className="small muted">Last few closes: {step.context.slice(-6).map((c: any) => `${c.session.slice(5)}: ${c.close}`).join(" · ")}</p>
              <div className="row">
                {["BUY", "SELL", "HOLD", "SKIP"].map((a) => (
                  <button key={a} disabled={!!guess} onClick={() => { setGuess(a); setScore((s) => ({ right: s.right + (a === step.answer.action ? 1 : 0), total: s.total + 1 })); }}>{a}</button>
                ))}
              </div>
              {guess && (
                <div className={`banner ${guess === step.answer.action ? "info" : "warn"}`} style={{ marginTop: ".75rem" }}>
                  <div>
                    <strong>{guess === step.answer.action ? "Correct." : `The stored result was ${step.answer.action}.`}</strong> {step.answer.reason}
                    {step.answer.execution && <div className="small">Next open: {step.answer.execution.status}{step.answer.execution.price ? ` at ${Number(step.answer.execution.price).toFixed(2)} USD` : ""}{step.answer.execution.reason ? ` — ${step.answer.execution.reason}` : ""}.</div>}
                    <div className="small muted">Following the rule correctly can still lose money: the next open can gap, and trend rules react late.</div>
                  </div>
                </div>
              )}
              <div className="row" style={{ marginTop: ".5rem" }}>
                <button disabled={i === 0} onClick={() => { setI(i - 1); setGuess(null); }}>← Previous</button>
                <button className="primary" disabled={i >= rep.steps.length - 1} onClick={() => { setI(i + 1); setGuess(null); }}>Next →</button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
