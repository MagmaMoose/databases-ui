import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, configure, type Environment, type Me } from "./api";
import { Activity } from "./views/Activity";
import { Audit } from "./views/Audit";
import { DatabaseView } from "./views/DatabaseView";
import { Databases } from "./views/Databases";

type Toast = { id: number; message: string; kind: "info" | "error" };

type Session = {
  me: Me;
  notify: (message: string, kind?: "info" | "error") => void;
};

const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside the signed-in shell");
  return session;
}

// The page in view lives in the hash: #/dev, #/dev/databases/payments, #/prd/activity
type Route = { path: string[] };

function routeFromHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  return { path: raw.split("?")[0].split("/").filter(Boolean).map(decodeURIComponent) };
}

export function go(path: string) {
  window.location.hash = path.startsWith("/") ? path : `/${path}`;
}

// The callback sends a sign-in error back as ?auth_error=; show it once and drop it from the URL.
function takeAuthError(): string {
  const params = new URLSearchParams(window.location.search);
  const error = params.get("auth_error") ?? "";
  if (params.has("auth_error")) window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  return error;
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<"loading" | "signed-out" | "ready" | "ended">("loading");
  const [authError] = useState(takeAuthError);
  const [route, setRoute] = useState(routeFromHash);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextToast = useRef(0);

  const notify = useCallback((message: string, kind: "info" | "error" = "info") => {
    const id = ++nextToast.current;
    setToasts((t) => [...t, { id, message, kind }]);
    if (kind === "info") window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);

  useEffect(() => {
    api
      .me()
      .then((data) => {
        configure(data.csrf, () => setState("ended"));
        setMe(data);
        setState("ready");
      })
      .catch((err) => setState(err instanceof ApiError && err.status === 401 ? "signed-out" : "ended"));
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  async function signOut() {
    try {
      await api.logout();
    } finally {
      setMe(null);
      setState("signed-out");
    }
  }

  if (state === "loading") return <main className="center muted">Loading…</main>;

  if (state === "signed-out" || state === "ended" || !me) {
    return (
      <main className="center">
        <div className="card sign-in">
          <Logo />
          <h1>Databases UI</h1>
          <p className="muted">{state === "ended" ? "Your session has ended." : "Sign in with your work account to snapshot, restore and clone your team's databases."}</p>
          {authError && (
            <p className="notice error" role="alert">
              {authError}
            </p>
          )}
          <a className="button primary wide" href="/auth/login">
            Sign in with Microsoft
          </a>
        </div>
      </main>
    );
  }

  const [envName, page = "", id] = route.path;
  const env = me.environments.find((e) => e.name === envName) ?? me.environments.find((e) => e.runs_jobs) ?? me.environments[0];
  const pages = [
    { match: "", label: "Databases" },
    { match: "activity", label: "Activity" },
    { match: "audit", label: "Audit log" },
  ];

  return (
    <SessionContext.Provider value={{ me, notify }}>
      <div className="shell">
        <header className="topbar">
          <a className="brand" href="#/">
            <Logo />
            <span>Databases UI</span>
          </a>
          <div className="who">
            <span title={me.upn}>{me.name || me.upn}</span>
            <button type="button" className="button small" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </header>
        <div className="layout">
          <nav className="vaults" aria-label="Environments">
            <select className="vault-select" aria-label="Environment" value={env.name} onChange={(e) => go(`/${e.target.value}`)}>
              {me.environments.map((e) => (
                <option key={e.name} value={e.name}>
                  {e.name}
                  {e.production ? " (production)" : ""}
                </option>
              ))}
            </select>
            <div className="nav-group">
              <p className="nav-title">Environments</p>
              <ul>
                {me.environments.map((e) => (
                  <li key={e.name}>
                    <a href={`#/${e.name}`} aria-current={e.name === env.name ? "page" : undefined}>
                      {e.name} {e.production && <span className="badge state-declined">production</span>}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
            {env.runs_jobs && (
              <div className="nav-group">
                <p className="nav-title">{env.name}</p>
                <ul>
                  {pages.map((p) => (
                    <li key={p.match}>
                      <a href={`#/${env.name}${p.match ? `/${p.match}` : ""}`} aria-current={(page === "databases" ? "" : page) === p.match ? "page" : undefined}>
                        {p.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <p className="muted small version">
              {me.release_notes ? (
                <a href={me.release_notes} target="_blank" rel="noreferrer" title="Release notes">
                  {me.version}
                </a>
              ) : (
                me.version
              )}
              {me.auditor && !me.admin && " · read-only"}
            </p>
          </nav>
          <main className="content">
            <Page env={env} page={page} id={id} />
          </main>
        </div>
        <div className="toasts" role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast ${t.kind}`}>
              <span>{t.message}</span>
              <button type="button" className="icon-button" aria-label="Dismiss" onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}>
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
    </SessionContext.Provider>
  );
}

function Page({ env, page, id }: { env: Environment; page: string; id?: string }) {
  if (page === "databases" && id) return <DatabaseView key={`${env.name}/${id}`} env={env} name={id} />;
  if (page === "activity") return <Activity key={env.name} env={env} />;
  if (page === "audit") return <Audit key={env.name} env={env} />;
  if (page === "" || page === "databases") return <Databases key={env.name} env={env} />;
  return (
    <div className="card">
      <h1>Nothing here</h1>
      <p className="muted">
        That page does not exist. <a href={`#/${env.name}`}>Back to the databases</a>.
      </p>
    </div>
  );
}

export function Section({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

function Logo() {
  return (
    <svg className="logo" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" />
      <ellipse cx="16" cy="10" rx="7" ry="3" />
      <path d="M9 10v12c0 1.7 3.1 3 7 3s7-1.3 7-3V10M9 16c0 1.7 3.1 3 7 3s7-1.3 7-3" />
    </svg>
  );
}
