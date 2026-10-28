import { useEffect, useState, type FormEvent } from "react";
import { api, type Database, type Environment, type Snapshot } from "../api";
import { go, Section, useSession } from "../App";
import { Confirm, Dialog } from "../components/Dialog";
import { Menu } from "../components/Menu";
import { Loading, message, useLoad } from "../components/ui";
import { bytes, snapshotTime } from "../format";

type Open = null | { kind: "snapshot" } | { kind: "script" } | { kind: "restore"; snapshot?: string } | { kind: "share"; snapshot: Snapshot } | { kind: "drop" };

export function DatabaseView({ env, name }: { env: Environment; name: string }) {
  const { notify } = useSession();
  const databases = useLoad(() => api.databases(env.name), [env.name]);
  const snapshots = useLoad(() => api.snapshots(env.name, name), [env.name, name]);
  const [dialog, setDialog] = useState<Open>(null);
  const [busy, setBusy] = useState(false);
  const db = databases.data?.find((d) => d.name === name);

  if (!databases.data) return <Loading error={databases.error} />;
  if (!db)
    return (
      <div className="card">
        <h1>{name}</h1>
        <p className="muted">
          {env.name} has no database {name} in a namespace you can see. <a href={`#/${env.name}`}>Back to the databases</a>.
        </p>
      </div>
    );

  async function dropCopy() {
    setBusy(true);
    try {
      await api.dropCopy(env.name, name);
      notify(`${db?.copy?.name} is being dropped.`);
      setDialog(null);
      databases.reload();
    } catch (err) {
      notify(message(err), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>{db.name}</h1>
        <p className="muted">
          {env.name} · namespace <span className="mono">{db.namespace}</span> · {db.server ? "as" : "owned by"} <span className="mono">{db.owner}</span>
          {db.owner_secret && (
            <>
              {" "}
              (password in Secret <span className="mono">{db.owner_secret}</span>)
            </>
          )}
          {db.server && (
            <>
              {" "}
              · external, <span className="mono">{`${db.server.host}:${db.server.port}/${db.server.database}`}</span>
            </>
          )}
        </p>
        {(db.can_snapshot || db.can_restore || db.can_script) && (
          <div className="actions">
            {db.can_snapshot && (
              <button type="button" className="button" onClick={() => setDialog({ kind: "snapshot" })}>
                Take a snapshot
              </button>
            )}
            {db.can_script && (
              <button type="button" className={db.can_restore ? "button" : "button primary"} onClick={() => setDialog({ kind: "script" })}>
                Run a script…
              </button>
            )}
            {db.can_restore && (
              <button type="button" className="button primary" onClick={() => setDialog({ kind: "restore" })}>
                Restore…
              </button>
            )}
          </div>
        )}
      </div>
      {!db.ready && <p className="notice warn">CloudNativePG has not applied this database: {db.message || "no reason given"}.</p>}
      {db.server && (
        <p className="notice">
          An external database: snapshots and scripts run here as <span className="mono">{db.owner}</span>. Restoring one of its snapshots happens outside this app.
        </p>
      )}
      {db.copy && (
        <p className="notice">
          A copy, <span className="mono">{db.copy.name}</span>, holds a restored snapshot{db.copy.ready ? "" : " (being created)"}.{" "}
          {db.can_restore && (
            <button type="button" className="button small danger-text" onClick={() => setDialog({ kind: "drop" })}>
              Drop the copy
            </button>
          )}
        </p>
      )}
      <Section title="Snapshots">
        {!snapshots.data ? (
          <Loading error={snapshots.error} />
        ) : snapshots.data.length === 0 ? (
          <div className="empty">
            <p>No snapshots of {name} yet.</p>
          </div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Taken</th>
                <th>By</th>
                <th>Size</th>
                <th>Note</th>
                <th className="actions-cell" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {snapshots.data.map((s) => {
                const items = [
                  ...(db.can_restore ? [{ label: "Restore…", onSelect: () => setDialog({ kind: "restore", snapshot: s.id }) }] : []),
                  ...(db.can_snapshot && env.downloads ? [{ label: "Download", onSelect: () => (window.location.href = api.downloadUrl(env.name, name, s.id)) }] : []),
                  ...(db.can_snapshot && env.shares_to.length > 0 ? [{ label: "Share with another environment…", onSelect: () => setDialog({ kind: "share", snapshot: s }) }] : []),
                ];
                return (
                  <tr key={s.id}>
                    <td title={s.id}>{snapshotTime(s.id)}</td>
                    <td>{s.created_by || "—"}</td>
                    <td>{bytes(s.size)}</td>
                    <td>
                      {s.note || <span className="muted">—</span>}
                      {s.shared_with.length > 0 && <div className="muted small">shared with {s.shared_with.join(", ")}</div>}
                    </td>
                    <td className="actions-cell">{items.length > 0 && <Menu label="Actions" items={items} />}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Section>
      {db.workloads.length > 0 && (
        <Section title="Workloads">
          <p className="muted small">
            A restore in place scales these, in <span className="mono">{db.namespace}</span>, to 0 replicas, and back to what they had afterwards.
          </p>
          <ul className="list mono small">
            {db.workloads.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Section>
      )}
      {dialog?.kind === "snapshot" && (
        <SnapshotDialog
          env={env}
          db={db}
          onClose={() => setDialog(null)}
          onDone={() => {
            setDialog(null);
            notify(`Taking a snapshot of ${name}; it shows here when it is done.`);
            go(`/${env.name}/activity`);
          }}
        />
      )}
      {dialog?.kind === "script" && (
        <ScriptDialog
          env={env}
          db={db}
          onClose={() => setDialog(null)}
          onDone={(waiting, mode) => {
            setDialog(null);
            notify(waiting ? "The script waits for a second person to approve it." : mode === "check" ? "Checking the script; its output shows under Activity." : "The script is running; its output shows under Activity.");
            go(`/${env.name}/activity`);
          }}
        />
      )}
      {dialog?.kind === "restore" && (
        <RestoreDialog
          env={env}
          db={db}
          snapshot={dialog.snapshot}
          onClose={() => setDialog(null)}
          onDone={(waiting) => {
            setDialog(null);
            notify(waiting ? "The restore waits for a second person to approve it." : "The restore has started.");
            go(`/${env.name}/activity`);
          }}
        />
      )}
      {dialog?.kind === "share" && (
        <ShareDialog
          env={env}
          db={db}
          snapshot={dialog.snapshot}
          onClose={() => setDialog(null)}
          onDone={(target) => {
            setDialog(null);
            notify(`Shared with ${target}.`);
            snapshots.reload();
          }}
        />
      )}
      {dialog?.kind === "drop" && db.copy && (
        <Confirm title={`Drop ${db.copy.name}?`} action="Drop the copy" danger busy={busy} onConfirm={() => void dropCopy()} onClose={() => setDialog(null)}>
          <p>CloudNativePG drops the database and everything in it. Snapshots are not affected.</p>
        </Confirm>
      )}
    </>
  );
}

function SnapshotDialog({ env, db, onClose, onDone }: { env: Environment; db: Database; onClose: () => void; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.snapshot(env.name, db.name, note);
      onDone();
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Snapshot of ${db.name}`} onClose={onClose}>
      <form className="form" onSubmit={(e) => void submit(e)}>
        <div className="dialog-body">
          <p className="muted">pg_dump writes the whole database to {env.name}'s storage. The database stays online.</p>
          <div className="field">
            <label htmlFor="note">Note (optional)</label>
            <input id="note" data-autofocus="" maxLength={300} value={note} onChange={(e) => setNote(e.target.value)} placeholder="before the 2.4 migration" />
          </div>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
        <footer className="dialog-footer">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy}>
            Take the snapshot
          </button>
        </footer>
      </form>
    </Dialog>
  );
}

function ScriptDialog({ env, db, onClose, onDone }: { env: Environment; db: Database; onClose: () => void; onDone: (waiting: boolean, mode: "check" | "run") => void }) {
  const [script, setScript] = useState("");
  const [mode, setMode] = useState<"check" | "run">("check");
  const [snapshot, setSnapshot] = useState(true);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ready = script.trim() !== "" && reason.trim().length >= 3;

  async function load(file: File | undefined) {
    if (!file) return;
    if (file.size > 100_000) {
      setError(`${file.name} is over 100 kB; split it.`);
      return;
    }
    setError("");
    setScript(await file.text());
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const job = await api.script(env.name, db.name, { script, mode, reason: reason.trim(), snapshot: mode === "run" && snapshot });
      onDone(job.state === "waiting", mode);
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Run a script on ${db.name}`} wide onClose={onClose}>
      <form className="form" onSubmit={(e) => void submit(e)}>
        <div className="dialog-body">
          <div className="field">
            <div className="label-row">
              <label htmlFor="script">SQL</label>
              <label className="button small file-button">
                Open a .sql file…
                <input type="file" accept=".sql,text/plain" className="visually-hidden" onChange={(e) => void load(e.target.files?.[0])} />
              </label>
            </div>
            <textarea
              id="script"
              className="mono script"
              data-autofocus=""
              rows={10}
              spellCheck={false}
              value={script}
              onChange={(e) => setScript(e.target.value)}
              placeholder={"insert into menu (code, label) values ('A', 'Results');\nupdate menu set position = 10 where code = 'A';"}
            />
            <p className="hint">
              It runs as <span className="mono">{db.owner}</span>, in one transaction: every statement or none. End each statement with a semicolon, and leave out BEGIN, COMMIT and psql
              commands. A lock it can't get within 15 seconds stops it.
            </p>
          </div>
          <fieldset className="field">
            <legend>How</legend>
            <ul className="choices">
              <li>
                <label className={mode === "check" ? "choice chosen" : "choice"}>
                  <input type="radio" name="mode" checked={mode === "check"} onChange={() => setMode("check")} />
                  <span>
                    <strong>Check</strong>: run it, show what it did, then roll it back
                  </span>
                </label>
              </li>
              <li>
                <label className={mode === "run" ? "choice chosen" : "choice"}>
                  <input type="radio" name="mode" checked={mode === "run"} onChange={() => setMode("run")} />
                  <span>
                    <strong>Run</strong>: commit it
                  </span>
                </label>
              </li>
            </ul>
          </fieldset>
          {mode === "run" && (
            <label className="check">
              <input type="checkbox" checked={snapshot} onChange={(e) => setSnapshot(e.target.checked)} />
              Take a snapshot first, to go back to
            </label>
          )}
          <div className="field">
            <label htmlFor="script-reason">Why</label>
            <input id="script-reason" required minLength={3} maxLength={300} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="the menu items for the next release" />
          </div>
          <p className="hint">
            A copy of the script and who ran it are kept in {env.name}'s audit log.
            {env.production && " In production a second person approves it, after reading it, before it starts; the output shows statement results, not rows."}
          </p>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
        <footer className="dialog-footer">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={mode === "run" && !env.production ? "button danger" : "button primary"} disabled={busy || !ready}>
            {env.production ? "Ask for approval" : mode === "check" ? "Check the script" : "Run the script"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}

// dev-payments shared with acc suggests acc-payments, for namespaces that start with their environment.
function namespaceIn(namespace: string, from: string, to: string): string {
  return namespace.startsWith(`${from}-`) ? `${to}-${namespace.slice(from.length + 1)}` : namespace;
}

function ShareDialog({ env, db, snapshot, onClose, onDone }: { env: Environment; db: Database; snapshot: Snapshot; onClose: () => void; onDone: (target: string) => void }) {
  const [target, setTarget] = useState(env.shares_to[0]);
  const [namespace, setNamespace] = useState(namespaceIn(db.namespace, env.name, env.shares_to[0]));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.share(env.name, db.name, snapshot.id, { environment: target, namespace: namespace.trim() });
      onDone(`${namespace.trim()} in ${target}`);
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Share the snapshot of ${snapshotTime(snapshot.id)}`} onClose={onClose}>
      <form className="form" onSubmit={(e) => void submit(e)}>
        <div className="dialog-body">
          <p className="muted">Whoever may restore databases in that namespace can then clone this snapshot into one of them. It holds all of {db.name}'s data.</p>
          <div className="form-row">
            <div className="field">
              <label htmlFor="share-env">Environment</label>
              <select
                id="share-env"
                value={target}
                onChange={(e) => {
                  setTarget(e.target.value);
                  setNamespace(namespaceIn(db.namespace, env.name, e.target.value));
                }}
              >
                {env.shares_to.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="share-ns">Namespace there</label>
              <input id="share-ns" className="mono" required pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?" value={namespace} onChange={(e) => setNamespace(e.target.value)} />
            </div>
          </div>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
        <footer className="dialog-footer">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy || !namespace.trim()}>
            Share
          </button>
        </footer>
      </form>
    </Dialog>
  );
}

function RestoreDialog({ env, db, snapshot, onClose, onDone }: { env: Environment; db: Database; snapshot?: string; onClose: () => void; onDone: (waiting: boolean) => void }) {
  const [source, setSource] = useState(env.name);
  const [list, setList] = useState<Snapshot[] | null>(null);
  const [chosen, setChosen] = useState(snapshot ?? "");
  const [into, setInto] = useState<"database" | "copy">("database");
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // This database's own snapshots here, or what another environment shared with its namespace.
  useEffect(() => {
    let live = true;
    setList(null);
    setError("");
    const load = source === env.name ? api.snapshots(env.name, db.name) : api.shared(env.name, db.name).then((all) => all.filter((s) => s.environment === source));
    load.then(
      (found) => {
        if (!live) return;
        setList(found);
        setChosen((c) => (found.some((s) => s.id === c) ? c : (found[0]?.id ?? "")));
      },
      (err) => live && setError(message(err)),
    );
    return () => {
      live = false;
    };
  }, [source, env.name, db.name]);

  const picked = list?.find((s) => s.id === chosen);
  const inPlace = into === "database";
  const ready = picked && reason.trim().length >= 3 && (!inPlace || typed === db.name);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!picked) return;
    setBusy(true);
    setError("");
    try {
      const job = await api.restore(env.name, db.name, { source_environment: source, source_database: picked.database, snapshot: picked.id, into, reason: reason.trim() });
      onDone(job.state === "waiting");
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Restore into ${env.name}`} wide onClose={onClose}>
      <form className="form" onSubmit={(e) => void submit(e)}>
        <div className="dialog-body">
          <div className="form-row">
            <div className="field">
              <label htmlFor="source">From</label>
              <select id="source" value={source} onChange={(e) => setSource(e.target.value)}>
                {[env.name, ...env.clone_from].map((s) => (
                  <option key={s} value={s}>
                    {s === env.name ? `${s}, this database's snapshots` : `${s}, shared with ${db.namespace}`}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="snapshot">Snapshot</label>
              <select id="snapshot" value={chosen} onChange={(e) => setChosen(e.target.value)} disabled={!list || list.length === 0}>
                {list?.length === 0 && <option value="">{source === env.name ? "No snapshots" : "Nothing shared with this namespace"}</option>}
                {(list ?? []).map((s) => (
                  <option key={`${s.database}/${s.id}`} value={s.id}>
                    {source === env.name ? "" : `${s.database} · `}
                    {snapshotTime(s.id)} · {bytes(s.size)} · {s.created_by}
                    {s.note ? ` · ${s.note}` : ""}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <fieldset className="field">
            <legend>Into</legend>
            <ul className="choices">
              <li>
                <label className={inPlace ? "choice chosen" : "choice"}>
                  <input type="radio" name="into" checked={inPlace} onChange={() => setInto("database")} />
                  <span>
                    <strong className="mono">{db.name}</strong>, replacing everything its owner has there
                    {db.workloads.length > 0 && <span className="muted small"> · scales {db.workloads.join(", ")} to 0 while it runs</span>}
                  </span>
                </label>
              </li>
              <li>
                <label className={!inPlace ? "choice chosen" : "choice"}>
                  <input type="radio" name="into" checked={!inPlace} onChange={() => setInto("copy")} />
                  <span>
                    <strong className="mono">{db.name}_copy</strong>, a separate database next to it{db.copy ? ", replacing the copy there" : ""}
                  </span>
                </label>
              </li>
            </ul>
          </fieldset>
          <div className="field">
            <label htmlFor="reason">Why</label>
            <input id="reason" required minLength={3} maxLength={300} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="roll back the failed 2.4 migration" />
          </div>
          {inPlace && (
            <div className="field">
              <label htmlFor="typed">
                Type <span className="mono">{db.name}</span> to replace it
              </label>
              <input id="typed" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} aria-invalid={typed !== "" && typed !== db.name} />
            </div>
          )}
          <p className="hint">
            One transaction: the database ends up as the snapshot was, or if anything fails, as it is now.
            {env.production && " In production a second person approves it before it starts."}
          </p>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
        <footer className="dialog-footer">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={inPlace ? "button danger" : "button primary"} disabled={busy || !ready}>
            {env.production ? "Ask for approval" : inPlace ? `Replace ${db.name}` : "Restore into the copy"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}
