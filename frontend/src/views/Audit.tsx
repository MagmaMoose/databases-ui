import { api, type AuditEntry, type Environment } from "../api";
import { Loading, useLoad } from "../components/ui";
import { snapshotTime, when } from "../format";

const ACTIONS: Record<string, string> = {
  "snapshot.requested": "asked for a snapshot",
  "snapshot.succeeded": "snapshot taken",
  "snapshot.failed": "snapshot failed",
  "snapshot.downloaded": "downloaded a snapshot",
  "restore.requested": "asked for a restore",
  "restore.approved": "approved a restore",
  "restore.declined": "declined a restore",
  "restore.expired": "restore request expired",
  "restore.succeeded": "restore done",
  "restore.failed": "restore failed",
  "copy.dropped": "dropped a copy",
};

// "acc/snapshots/payments_acc/<id>.dump" reads as "acc/payments_acc, snapshot of <the time it was taken>".
function detail(e: AuditEntry, env: string): string {
  const parts: string[] = [];
  const snap = /^(?:([a-z0-9-]+)\/)?snapshots\/([^/]+)\/([^/]+)\.dump$/.exec(e.snapshot ?? "");
  if (snap) parts.push(`${snap[1] && snap[1] !== env ? `${snap[1]}/${snap[2]}, ` : ""}snapshot of ${snapshotTime(snap[3])}`);
  if (e.target) parts.push(e.action === "copy.dropped" ? e.target : `into ${e.target}`);
  if (e.detail) parts.push(e.detail);
  return parts.join(" · ");
}

export function Audit({ env }: { env: Environment }) {
  const { data, error } = useLoad(() => api.audit(env.name), [env.name]);

  return (
    <>
      <div className="page-head">
        <h1>Audit log of {env.name}</h1>
        <p className="muted">Who asked for what, who approved it and how it ended, over the last two months.</p>
      </div>
      {!data ? (
        <Loading error={error} />
      ) : data.length === 0 ? (
        <div className="empty">
          <p>Nothing recorded yet.</p>
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>What</th>
              <th>Database</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {data.map((e, i) => (
              <tr key={`${e.at}-${i}`}>
                <td className="nowrap">{when(e.at)}</td>
                <td>{e.actor}</td>
                <td title={e.job}>{ACTIONS[e.action] ?? e.action}</td>
                <td className="mono">{e.database ?? ""}</td>
                <td className="small">{detail(e, env.name)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
