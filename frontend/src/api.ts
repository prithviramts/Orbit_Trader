// Minimal API client. The session lives in an HttpOnly cookie (JS cannot read it).
// The CSRF token is held in memory only — never in localStorage. No broker or notification
// secret is ever sent to the browser.
let csrf: string | null = null;

export function setCsrf(token: string | null) {
  csrf = token;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function handle(r: Response) {
  if (r.status === 401) {
    window.dispatchEvent(new CustomEvent("orbit:unauthorized"));
  }
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await r.json() : await r.text();
  if (!r.ok) {
    const msg = typeof body === "string" ? body : typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    throw new ApiError(r.status, msg);
  }
  return body;
}

export async function get<T = any>(path: string): Promise<T> {
  return handle(await fetch(path, { credentials: "same-origin" }));
}

export async function post<T = any>(path: string, body?: unknown): Promise<T> {
  const r = await handle(
    await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": csrf } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  );
  window.dispatchEvent(new CustomEvent("orbit:changed")); // refresh status views right after any action
  return r;
}

export async function postForm<T = any>(path: string, form: FormData): Promise<T> {
  return handle(
    await fetch(path, { method: "POST", credentials: "same-origin", headers: csrf ? { "X-CSRF-Token": csrf } : {}, body: form })
  );
}

export type MoneyPair = { usd: string; aed: string; usd_exact?: string } | null;
