import { api, type Environment } from "../api";
import { useSession } from "../App";
import { Loading, useLoad } from "../components/ui";

export function Databases({ env }: { env: Environment }) {
  const { me } = useSession();
  const { data, error } = useLoad(() => api.databases(env.name), [env.name]);
  const cloners = me.environments.filter((e) => e.clone_from.includes(env.name)).map((e) => e.name);

  return (
    <>
      <div className="page-head">
        <h1>
          {env.name} {env.production && <span className="badge state-declined">production</span>}
        </h1>
        <p className="muted">
          {env.runs_jobs
            ? env.production
              ? "Take a snapshot at any time. A restore waits for a second person, and snapshots stay here."
              : "Take a snapshot, restore one, or clone one from another environment."
            : `Only this environment's snapshots are read here${cloners.length ? `, to clone into ${cloners.join(", ")}` : ""}. Nothing runs in ${env.name} from this app.`}
        </p>
      </div>
      {!data ? (
        <Loading error={error} />
      ) : data.length === 0 ? (
        <div className="empty">
          <p>No databases of yours here.</p>
          <p className="muted small">The catalog says which team acts on which database; ask the platform team to add yours.</p>
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Database</th>
              <th>Owner</th>
              <th>Teams</th>
              <th>Status</th>
              <th>Copy</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.name}>
                <td className="name">
                  <a href={`#/${env.name}/databases/${encodeURIComponent(d.name)}`} className="mono">
                    {d.name}
                  </a>
                </td>
                <td className="mono small">{d.owner || "—"}</td>
                <td>{d.teams.join(", ") || <span className="muted">—</span>}</td>
                <td>
                  {d.ready === null ? <span className="muted">snapshots only</span> : d.ready ? <span className="status on">ready</span> : <span className="status off" title={d.message}>not ready</span>}
                </td>
                <td className="mono small">{d.copy ? d.copy.name : <span className="muted">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
