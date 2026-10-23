import { useEffect, useState, type FormEvent } from "react";
import { api, type Database, type Environment, type Snapshot } from "../api";
import { go, Section, useSession } from "../App";
import { Confirm, Dialog } from "../components/Dialog";
import { Menu } from "../components/Menu";
import { Loading, message, useLoad } from "../components/ui";
import { bytes, snapshotTime } from "../format";

export function DatabaseView({ env, name }: { env: Environment; name: string }) {
  const { notify } = useSession();
  const databases = useLoad(() => api.databases(env.name), [env.name]);
  const snapshots = useLoad(() => api.snapshots(env.name, name), [env.name, name]);
  const [dialog, setDialog] = useState<null | { kind: "snapshot" } | { kind: "restore"; snapshot?: string } | { kind: "drop" }>(null);
  const [busy, setBusy] = useState(false);
  const db = databases.data?.find((d) => d.name === name);

  if (!databases.data) return <Loading error={databases.error} />;
  if (!db)
    return (
      <div className="card">
        <h1>{name}</h1>
        <p className="muted">
          {env.name} has no database {name} of yours. <a href={`#/${env.name}`}>Back to the databases</a>.
        </p>
      </div>
    );

  const act = db.can_act;

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
          {env.name}
          {db.owner && <> · owned by <span className="mono">{db.owner}</span></>}
          {db.teams.length > 0 && <> · {db.teams.join(", ")}</>}
        </p>
        {act && (
          <div className="actions">
            <button type="button" className="button" onClick={() => setDialog({ kind: "snapshot" })}>
              Take a snapshot
            </button>
            <button type="button" className="button primary" onClick={() => setDialog({ kind: "restore" })}>
              Restore…
            </button>
          </div>
        )}
      </div>
      {db.ready === false && <p className="notice warn">CloudNativePG has not applied this database: {db.message || "no reason given"}.</p>}
      {db.copy && (
        <p className="notice">
          A copy, <span className="mono">{db.copy.name}</span>, holds a restored snapshot{db.copy.ready ? "" : " (being created)"}.{" "}
          {act && (
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
              {snapshots.data.map((s) => (
                <tr key={s.id}>
                  <td title={s.id}>{snapshotTime(s.id)}</td>
                  <td>{s.created_by || "—"}</td>
                  <td>{bytes(s.size)}</td>
                  <td>{s.note || <span className="muted">—</span>}</td>
                  <td className="actions-cell">
                    {act && (
                      <Menu
                        label="Actions"
                        items={[
                          { label: "Restore…", onSelect: () => setDialog({ kind: "restore", snapshot: s.id }) },
                          ...(env.downloads ? [{ label: "Download", onSelect: () => (window.location.href = api.downloadUrl(env.name, name, s.id)) }] : []),
                        ]}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      {db.workloads.length > 0 && (
        <Section title="Workloads">
          <p className="muted small">A restore in place scales these to 0 replicas, and back to what they had afterwards.</p>
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

function RestoreDialog({ env, db, snapshot, onClose, onDone }: { env: Environment; db: Database; snapshot?: string; onClose: () => void; onDone: (waiting: boolean) => void }) {
  const [source, setSource] = useState(env.name);
  // The databases of the source environment, and the one picked there: the same name if it has one.
  const [sources, setSources] = useState<{ env: string; databases: Database[]; picked: string } | null>({ env: env.name, databases: [db], picked: db.name });
  const [list, setList] = useState<Snapshot[] | null>(null);
  const [chosen, setChosen] = useState(snapshot ?? "");
  const [into, setInto] = useState<"database" | "copy">("database");
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sourceDb = sources?.env === source ? sources.picked : "";

  useEffect(() => {
    if (source === env.name) {
      setSources({ env: env.name, databases: [db], picked: db.name });
      return;
    }
    let live = true;
    setSources(null);
    setError("");
    api.databases(source).then(
      (found) => live && setSources({ env: source, databases: found, picked: found.some((d) => d.name === db.name) ? db.name : (found[0]?.name ?? "") }),
      (err) => live && setError(message(err)),
    );
    return () => {
      live = false;
    };
  }, [source, env.name, db]);

  useEffect(() => {
    setList(null);
    if (!sourceDb) return;
    let live = true;
    setError("");
    api.snapshots(source, sourceDb).then(
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
  }, [source, sourceDb]);

  const inPlace = into === "database";
  const ready = chosen && reason.trim().length >= 3 && (!inPlace || typed === db.name);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const job = await api.restore(env.name, db.name, { source_environment: source, source_database: sourceDb, snapshot: chosen, into, reason: reason.trim() });
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
                    {s === env.name ? `${s} (this environment)` : s}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="source-db">Database</label>
              <select id="source-db" value={sourceDb} onChange={(e) => sources && setSources({ ...sources, picked: e.target.value })} disabled={!sources || sources.env !== source}>
                {(sources?.databases ?? []).map((d) => (
                  <option key={d.name} value={d.name}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label htmlFor="snapshot">Snapshot</label>
            <select id="snapshot" value={chosen} onChange={(e) => setChosen(e.target.value)} disabled={!list || list.length === 0}>
              {list?.length === 0 && <option value="">No snapshots</option>}
              {(list ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {snapshotTime(s.id)} · {bytes(s.size)} · {s.created_by}
                  {s.note ? ` · ${s.note}` : ""}
                </option>
              ))}
            </select>
          </div>
          <fieldset className="field">
            <legend>Into</legend>
            <ul className="choices">
              <li>
                <label className={inPlace ? "choice chosen" : "choice"}>
                  <input type="radio" name="into" checked={inPlace} onChange={() => setInto("database")} />
                  <span>
                    <strong className="mono">{db.name}</strong>, replacing what is in it
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
            pg_restore runs in one transaction: if anything fails, nothing changes. Tables made after the snapshot was taken stay as they are.
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
