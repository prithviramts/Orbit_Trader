import { Badge, ErrorNote, Loading, UsdAed, usePoll } from "../components";

/** AI day trader (IVV, paper only): today's decision, its record, and how its model tested on unseen years. */
export function DayTrader() {
  const { data, error } = usePoll<any>("/api/day", 60000);
  if (error) return <ErrorNote error={error} />;
  if (!data) return <Loading />;
  const st = data.settings ?? {};
  const ev = data.model?.evaluation;
  const trades: any[] = [...(data.trades ?? [])].reverse();
  const prob = data.latest?.probability;
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: ".5rem" }}>
        <h2 style={{ margin: 0 }}>AI day trader (IVV)</h2>
        <div className="row">
          <Badge kind="accent">Paper</Badge>
          {st.self_paused ? <Badge kind="warn">Paused itself</Badge> : st.enabled ? <Badge kind="ok">On</Badge> : <Badge kind="">Off</Badge>}
        </div>
      </div>
      <p className="muted small">
        On days the model expects a winning trade, it buys USD {st.budget_usd} of IVV (an S&amp;P 500 fund, same index as SPY) at about
        18:45 Dubai (09:45 New York), sells if IVV falls {Number(data.stop_pct).toFixed(2)}% below the buy price, and always sells
        10 minutes before the close. It never holds overnight. It retrains every week and pauses itself if its last 20 trades lose money.
      </p>
      {st.self_paused && <div className="banner warn"><span aria-hidden="true">⚠</span><div>{st.self_pause_reason}</div></div>}

      <h3>Latest decision</h3>
      {data.latest ? (
        <>
          <div className="row">
            <Badge kind={data.latest.action === "BUY" ? "ok" : ""}>{data.latest.action}</Badge>
            {prob != null && <Badge kind="">{Math.round(prob * 100)}% model score</Badge>}
            <span className="muted">Session {data.latest.session}</span>
          </div>
          <p className="small">{data.latest.reason}</p>
        </>
      ) : <p className="muted">No decision yet. The first one comes at the next US open.</p>}

      <h3>Record</h3>
      {data.count ? (
        <>
          <p>{data.count} trades, {data.wins} winners. Total: <UsdAed usd={data.pnl} /></p>
          <div className="table-wrap"><table className="small">
            <thead><tr><th>Day</th><th>Buy</th><th>Sell</th><th>Result</th></tr></thead>
            <tbody>{trades.map((t) => (
              <tr key={t.bought_at}>
                <td>{t.day}</td><td className="num">{t.buy_price}</td><td className="num">{t.sell_price}</td>
                <td><UsdAed usd={t.pnl} /> <span className={t.return_pct >= 0 ? "chg up" : "chg down"}>({t.return_pct >= 0 ? "+" : ""}{t.return_pct.toFixed(2)}%)</span></td>
              </tr>
            ))}</tbody>
          </table></div>
        </>
      ) : <p className="muted">No completed trades yet.</p>}

      <h3>How the model tested on years it had not seen</h3>
      {ev ? (
        <div className="table-wrap"><table className="small">
          <thead><tr><th>{ev.from.slice(0, 4)} to {ev.to.slice(0, 4)}, no costs</th><th>Return</th><th>Trades</th><th>Winners</th></tr></thead>
          <tbody>
            <tr><th scope="row">AI day trader</th><td>{ev.ai.total_return_pct}%</td><td className="num">{ev.ai.trades}</td><td>{ev.ai.win_pct}%</td></tr>
            <tr><th scope="row">Same trade every day</th><td>{ev.every_day.total_return_pct}%</td><td className="num">{ev.every_day.trades}</td><td>{ev.every_day.win_pct}%</td></tr>
            <tr><th scope="row">Just hold SPY</th><td>{ev.hold_pct}%</td><td className="num">1</td><td>—</td></tr>
          </tbody>
        </table></div>
      ) : <p className="muted">No trained model yet.</p>}
      {data.model && <p className="muted small">Model {data.model.version}, trained on {data.model.rows} days up to {data.model.trained_through}. Past results do not promise future ones.</p>}
    </div>
  );
}
