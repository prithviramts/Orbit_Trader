import React, { Suspense, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { get, post, setCsrf } from "./api";
import { Badge, clearPollCache, ErrorNote, Loading, ModeBadge, usePoll } from "./components";
import { Icon, IconName } from "./icons";
import { TickerChip } from "./ticker";

// Pages load on demand so the first screen appears quickly (charts are only downloaded when needed).
const lazyPage = <K extends string>(load: () => Promise<Record<K, React.ComponentType>>, name: K) =>
  React.lazy(() => load().then((m) => ({ default: m[name] })));
const Overview = lazyPage(() => import("./pages/Overview"), "Overview");
const Market = lazyPage(() => import("./pages/Market"), "Market");
const Decisions = lazyPage(() => import("./pages/Decisions"), "Decisions");
const DecisionDetail = lazyPage(() => import("./pages/Decisions"), "DecisionDetail");
const Orders = lazyPage(() => import("./pages/Decisions"), "Orders");
const Research = lazyPage(() => import("./pages/Research"), "Research");
const Learn = lazyPage(() => import("./pages/Learn"), "Learn");
const Journal = lazyPage(() => import("./pages/Journal"), "Journal");
const Operations = lazyPage(() => import("./pages/Ops"), "Operations");
const Security = lazyPage(() => import("./pages/Ops"), "Security");
const Setup = lazyPage(() => import("./pages/Ops"), "Setup");
const Status = lazyPage(() => import("./pages/Status"), "Status");
const News = lazyPage(() => import("./pages/News"), "News");
import "./styles.css";

function getTheme(): string {
  try {
    return localStorage.getItem("orbit-theme") || "dark"; // per-viewer convenience only
  } catch {
    return "dark";
  }
}

function Wordmark() {
  return (
    <div className="brand">
      <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
        <circle cx="16" cy="16" r="5.5" fill="var(--accent)" />
        <ellipse cx="16" cy="16" rx="14" ry="6" fill="none" stroke="var(--accent-2)" strokeWidth="1.8" transform="rotate(-20 16 16)" />
      </svg>
      <div>
        <div className="brand-name">Orbit Trading Lab</div>
        <div className="brand-sub">Paper research console</div>
      </div>
    </div>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const status = usePoll<any>("/api/auth/status", 0);
  return (
    <div className="login card">
      <Wordmark />
      <h1>Sign in</h1>
      {status.data && !status.data.password_set ? (
        <div className="banner warn">
          <span aria-hidden="true">!</span>
          <div>
            No password is set yet. In PowerShell, from the project folder, run <code>.\scripts\set-password.ps1</code>. The password is typed into
            a hidden prompt and stored only as an Argon2 hash. There is no default password.
          </div>
        </div>
      ) : null}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setErr(null);
          try {
            const r = await post<any>("/api/auth/login", { password: pw });
            setCsrf(r.csrf_token);
            setPw("");
            onDone();
          } catch (e: any) {
            setErr(e.message);
          }
        }}
      >
        <div className="field">
          <label htmlFor="pw">Dashboard password</label>
          <input id="pw" type="password" autoComplete="current-password" style={{ width: "100%" }} value={pw} onChange={(e) => setPw(e.target.value)} />
        </div>
        <ErrorNote error={err} />
        <button className="primary" type="submit" disabled={!pw}>Sign in</button>
      </form>
      <p className="muted small" style={{ marginTop: "1rem" }}>
        Local single-user app on 127.0.0.1. Paper trading only — there is no live-money route.
      </p>
    </div>
  );
}

function TopBar({ theme, setTheme, onLogout }: { theme: string; setTheme: (t: string) => void; onLogout: () => void }) {
  const ov = usePoll<any>("/api/overview", 15000);
  const o = ov.data;
  return (
    <header className="topbar">
      <TickerChip />
      <div className="pills" role="status" aria-live="polite">
        <Badge kind="accent" icon="◎">PAPER</Badge>
        {o && <ModeBadge mode={o.mode} />}
        {o?.flags.security_paused && <Badge kind="bad" icon="⛔">SECURITY PAUSED</Badge>}
        {o?.flags.risk_paused_entries && <Badge kind="warn" icon="⏸">ENTRIES PAUSED (RISK)</Badge>}
        {o?.flags.new_orders_paused && <Badge kind="warn" icon="⏸">NEW ORDERS PAUSED</Badge>}
        {o?.flags.review_required && <Badge kind="bad" icon="!">REVIEW REQUIRED</Badge>}
        {o && (
          <Badge kind={o.worker.state === "RUNNING" ? "ok" : "bad"} icon={o.worker.state === "RUNNING" ? "●" : "○"} title={o.worker.text}>
            Worker {o.worker.state.replaceAll("_", " ").toLowerCase()}
          </Badge>
        )}
        {o && (
          <Badge kind={o.market.is_open_by_calendar ? "ok" : ""} icon={o.market.is_open_by_calendar ? "●" : "☾"}>
            Market {o.market.is_open_by_calendar ? "open" : "closed"}
          </Badge>
        )}
      </div>
      <span className="spacer" />
      <button className="icon-btn" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle light/dark theme" title="Light/dark theme">
        <Icon name={theme === "dark" ? "sun" : "moon"} />
      </button>
      <button className="ghost" onClick={onLogout}><Icon name="logout" /> Sign out</button>
    </header>
  );
}

type NavItem = [string, IconName, string];

function Sidebar({ groups }: { groups: [string, NavItem[]][] }) {
  const [open, setOpen] = useState(false);
  return (
    <aside className={`sidebar ${open ? "menu-open" : ""}`}>
      <div className="sidebar-head">
        <Wordmark />
        <button className="menu-toggle" aria-expanded={open} aria-controls="mainnav" onClick={() => setOpen(!open)}>
          <Icon name={open ? "close" : "menu"} /> {open ? "Close" : "Menu"}
        </button>
      </div>
      <nav id="mainnav" className="nav" aria-label="Main" onClick={() => setOpen(false)}>
        {groups.map(([title, links]) => (
          <div key={title} className="nav-group">
            <div className="nav-title">{title}</div>
            {links.map(([to, icon, label]) => (
              <NavLink key={to} to={to} end={to === "/"}>
                <Icon name={icon} />
                {label}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}

function App() {
  const [auth, setAuth] = useState<"unknown" | "in" | "out">("unknown");
  const [theme, setThemeState] = useState(getTheme());
  const setTheme = (t: string) => {
    setThemeState(t);
    try {
      localStorage.setItem("orbit-theme", t);
    } catch {
      /* storage may be blocked; theme still applies for this view */
    }
  };
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  const check = async () => {
    try {
      const s = await get<any>("/api/auth/status");
      setCsrf(s.csrf_token);
      setAuth(s.authenticated ? "in" : "out");
    } catch {
      setAuth("out");
    }
  };
  useEffect(() => {
    check();
    const h = () => { clearPollCache(); setAuth("out"); };
    window.addEventListener("orbit:unauthorized", h);
    return () => window.removeEventListener("orbit:unauthorized", h);
  }, []);
  if (auth === "unknown") return <div style={{ padding: "2rem" }}><Loading /></div>;
  if (auth === "out") return <Login onDone={check} />;
  const groups: [string, NavItem[]][] = [
    ["Trade", [["/", "home", "Overview"], ["/market", "candles", "Market & live chart"], ["/orders", "swap", "Orders & positions"], ["/decisions", "list", "Decisions"], ["/news", "news", "News & forecasts"]]],
    ["Learn", [["/research", "flask", "Research"], ["/learn", "book", "Learn"], ["/journal", "pen", "Journal & costs"]]],
    ["System", [["/status", "phone", "Status (phone)"], ["/setup", "plug", "Setup"], ["/ops", "gear", "Operations"], ["/security", "shield", "Security"]]],
  ];
  return (
    <BrowserRouter>
      <div className="shell">
        <Sidebar groups={groups} />
        <main className="main">
          <TopBar
            theme={theme}
            setTheme={setTheme}
            onLogout={async () => {
              try {
                await post("/api/auth/logout");
              } finally {
                setCsrf(null);
                clearPollCache();
                setAuth("out");
              }
            }}
          />
          <PageGuard>
          <Suspense fallback={<Loading />}>
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/status" element={<Status />} />
            <Route path="/setup" element={<Setup />} />
            <Route path="/market" element={<Market />} />
            <Route path="/decisions" element={<Decisions />} />
            <Route path="/decisions/:id" element={<DecisionDetail />} />
            <Route path="/orders" element={<Orders />} />
            <Route path="/research" element={<Research />} />
            <Route path="/learn" element={<Learn />} />
            <Route path="/journal" element={<Journal />} />
            <Route path="/news" element={<News />} />
            <Route path="/ops" element={<Operations />} />
            <Route path="/security" element={<Security />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
          </Suspense>
          </PageGuard>
        </main>
      </div>
    </BrowserRouter>
  );
}

/** Shows a visible error instead of a blank screen if a page crashes; resets when you change page. */
class PageErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error) {
    console.error("Orbit page crashed:", error);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="banner bad" role="alert">
        <span aria-hidden="true">!</span>
        <div>
          <strong>This page could not be shown.</strong> Trading is not affected: the worker runs separately.
          <div className="small mono">{String(this.state.error.message || this.state.error)}</div>
          <button onClick={() => window.location.reload()}>Reload page</button>
        </div>
      </div>
    );
  }
}

function PageGuard({ children }: { children: React.ReactNode }) {
  const loc = useLocation();
  return <PageErrorBoundary key={loc.pathname}>{children}</PageErrorBoundary>;
}

function NotFound() {
  const nav = useNavigate();
  return (
    <div className="card">
      <h2>Page not found</h2>
      <button onClick={() => nav("/")}>Go to overview</button>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
