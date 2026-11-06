import { useEffect, useRef, useState } from "react";
import { post } from "./api";
import { DubaiTime, usePoll } from "./components";
import { Icon } from "./icons";

/** Top-bar bell: the same alerts Orbit sends to your phone, kept inside the app. */
export function AlertsBell() {
  const { data, reload } = usePoll<any>("/api/events", 30000);
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const items: any[] = data?.items ?? [];
  const unread = items.filter((e) => !e.read).length;

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);

  const markRead = async () => {
    try { await post("/api/events/read"); reload(); } catch { /* shown again on next poll */ }
  };

  return (
    <div className="bell-wrap" ref={box}>
      <button className="icon-btn bell" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}
        aria-label={unread ? `Alerts, ${unread} unread` : "Alerts"} title="Alerts">
        <Icon name="bell" />
        {unread > 0 && <span className="bell-count" aria-hidden="true">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && (
        <div className="bell-panel" role="dialog" aria-label="Recent alerts">
          <div className="bell-head">
            <strong>Alerts</strong>
            {unread > 0 && <button className="ghost small" onClick={markRead}>Mark all read</button>}
          </div>
          {items.length === 0 && <div className="bell-item muted small">No alerts yet. Trades, problems and daily summaries appear here.</div>}
          {items.slice(0, 30).map((e) => {
            const long = e.body.length > 260;
            const show = expanded === e.id || !long ? e.body : e.body.slice(0, 260) + "…";
            return (
              <div key={e.id} className={`bell-item ${e.read ? "" : "unread"} ${e.severity}`}>
                <div className="t">{e.severity === "critical" ? "⛔ " : ""}{e.title}</div>
                <div className="muted small"><DubaiTime t={e.at} /></div>
                <div className="b">{show}</div>
                {long && <button className="ghost small" onClick={() => setExpanded(expanded === e.id ? null : e.id)}>{expanded === e.id ? "Less" : "More"}</button>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
