import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { get, MoneyPair } from "./api";

const RATE = 3.6725;

/** USD amount with its AED reference equivalent. Rounding is done server-side (ROUND_HALF_UP). */
export function Money({ v, sign = false }: { v: MoneyPair | undefined; sign?: boolean }) {
  if (!v) return <span className="muted">N/A</span>;
  const neg = v.usd.startsWith("-");
  const s = sign && !neg && Number(v.usd) > 0 ? "+" : "";
  return (
    <span className="money num" title={`Reference conversion at ${RATE} AED per USD; not a bank rate.`}>
      {s}USD {fmt(v.usd)}
      <span className="aed">≈ AED {fmt(v.aed)}</span>
    </span>
  );
}

/** For plain USD numbers that came without a server pair (e.g. backtest metrics). */
export function UsdAed({ usd }: { usd: string | number | null | undefined }) {
  if (usd === null || usd === undefined || usd === "") return <span className="muted">N/A</span>;
  const n = Number(usd);
  return <Money v={{ usd: roundHalfUp(n), aed: roundHalfUp(n * RATE) }} />;
}

function roundHalfUp(n: number): string {
  // Display-only helper; authoritative Decimal rounding happens on the server.
  const s = Math.sign(n) * Math.round(Math.abs(n) * 100 + 1e-9) / 100;
  return s.toFixed(2);
}

export function fmt(s: string) {
  const [i, d] = s.replace("-", "").split(".");
  const neg = s.startsWith("-") ? "-" : "";
  return neg + i.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (d !== undefined ? "." + d : "");
}

export function pct(v: string | number | null | undefined, dp = 2) {
  if (v === null || v === undefined) return "N/A";
  return (Number(v) * 100).toFixed(dp) + "%";
}

export function Badge({ kind, icon, children, title }: { kind: string; icon?: string; children: ReactNode; title?: string }) {
  return (
    <span className={`badge ${kind}`} title={title}>
      {icon && <span aria-hidden="true">{icon}</span>}
      {children}
    </span>
  );
}

export function Tip({ term, children }: { term: string; children: ReactNode }) {
  return (
    <span className="tip" title={term} tabIndex={0} aria-label={`${children}: ${term}`}>
      {children}
    </span>
  );
}

export function DubaiTime({ t, withNy = false }: { t?: { utc: string; dubai: string; new_york: string } | null; withNy?: boolean }) {
  if (!t) return <span className="muted">—</span>;
  const d = new Date(t.utc);
  const dub = d.toLocaleString("en-GB", { timeZone: "Asia/Dubai", weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const ny = d.toLocaleString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", month: "short", day: "2-digit" });
  return (
    <span title={`UTC ${t.utc}`}>
      {dub} <span className="muted small">Dubai</span>
      {withNy && <span className="muted small"> · {ny} New York</span>}
    </span>
  );
}

export function usePoll<T>(path: string | null, ms = 15000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await get<T>(path);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e: any) {
      if (alive.current) setError(e.message);
    }
  }, [path]);
  useEffect(() => {
    alive.current = true;
    load();
    const id = ms > 0 ? window.setInterval(load, ms) : undefined;
    window.addEventListener("orbit:changed", load);
    return () => {
      alive.current = false;
      window.removeEventListener("orbit:changed", load);
      if (id) window.clearInterval(id);
    };
  }, [load, ms]);
  return { data, error, reload: load };
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="banner bad" role="alert">
      <span aria-hidden="true">✖</span>
      <div>{error}</div>
    </div>
  );
}

export function Loading() {
  return <p className="muted">Loading…</p>;
}

/** Confirmation modal with an explanation of the effect; optional password field for sensitive actions. */
export function Confirm({
  title, children, onConfirm, onClose, needPassword, needNote, danger, confirmLabel = "Confirm",
}: {
  title: string; children: ReactNode; onConfirm: (pw: string, note: string) => Promise<void>; onClose: () => void;
  needPassword?: boolean; needNote?: string; danger?: boolean; confirmLabel?: string;
}) {
  const [pw, setPw] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <div className="modal-back" role="dialog" aria-modal="true" aria-label={title}>
      <div className="card modal">
        <h2>{title}</h2>
        <div>{children}</div>
        {needNote && (
          <div className="field">
            <label htmlFor="cnote">{needNote}</label>
            <textarea id="cnote" rows={3} style={{ width: "100%" }} value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} />
          </div>
        )}
        {needPassword && (
          <div className="field">
            <label htmlFor="cpw">Re-enter your dashboard password</label>
            <input id="cpw" type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} />
          </div>
        )}
        <ErrorNote error={err} />
        <div className="row">
          <button onClick={onClose}>Cancel</button>
          <button
            className={danger ? "danger" : "primary"}
            disabled={busy || (needPassword && !pw)}
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                await onConfirm(pw, note);
                onClose();
              } catch (e: any) {
                setErr(e.message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function SampleBanner() {
  return (
    <div className="banner sample" role="note">
      <Badge kind="sample" icon="◇">SAMPLE</Badge>
      <div>
        <strong>Synthetic prices.</strong> These numbers were generated offline for learning. They are not real SPY history, never place orders, and
        live in a separate ledger from paper-account activity.
      </div>
    </div>
  );
}

export function ModeBadge({ mode }: { mode: string }) {
  return mode === "PAPER_AUTO" ? (
    <Badge kind="warn" icon="▶" title="Automatic paper orders enabled">PAPER AUTOMATION ON</Badge>
  ) : (
    <Badge kind="accent" icon="◉" title="Read-only; no orders">OBSERVE</Badge>
  );
}

export function StateBadge({ state }: { state: string }) {
  const map: Record<string, [string, string]> = {
    FILLED: ["ok", "✔"], TERMINAL_PARTIAL: ["warn", "◐"], PENDING: ["info", "…"], SUBMITTING: ["info", "↗"], SUBMITTED: ["info", "↗"],
    PARTIALLY_FILLED: ["warn", "◐"], CANCEL_REQUESTED: ["warn", "⨯?"], AMBIGUOUS: ["bad", "?"], MISSED_EXECUTION: ["bad", "⏰"],
    REJECTED: ["bad", "✖"], CANCELLED_ZERO_FILL: ["", "⨯"], SKIPPED: ["", "–"], SUPERSEDED: ["", "↺"], REVIEW_REQUIRED: ["bad", "!"],
    BUY: ["ok", "▲"], SELL: ["warn", "▼"], HOLD: ["", "■"], SKIP: ["", "–"], ERROR: ["bad", "✖"],
  };
  const [k, i] = map[state] || ["", "•"];
  return <Badge kind={k} icon={i}>{state.replaceAll("_", " ")}</Badge>;
}
