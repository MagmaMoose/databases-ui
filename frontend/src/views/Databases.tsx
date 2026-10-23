import { api, type Environment } from "../api";
import { useSession } from "../App";
import { Loading, useLoad } from "../components/ui";

export function Databases({ env }: { env: Environment }) {
  const { me } = useSession();
  const { data, error } = useLoad(() => api.databases(env.name), [env.name]);

  return (
    <>
      <div className="page-head">
        <h1>
          {env.name} {env.production && <span className="badge state-declined">production</span>}
        </h1>
        <p className="muted">
          {env.production
            ? "Take a snapshot at any time. A restore waits for a second person, and snapshots stay here."
            : "Take a snapshot, restore one, or clone one another environment shared with you."}{" "}
          You see the databases of the namespaces Kubernetes lets you into.
        </p>
      </div>
      {!data ? (
        <Loading error={error} />
      ) : data.length === 0 ? (
        <div className="empty">
          <p>No databases in namespaces you can see.</p>
          <p className="muted small">
            Kubernetes knows you as <span className="mono">{me.kubernetes.user}</span>
            {me.kubernetes.groups.length > 0 ? (
              <>
                {" "}
                in <span className="mono">{me.kubernetes.groups.join(", ")}</span>
              </>
            ) : (
              " in no groups"
            )}
            . Ask for access to your app's namespace.
          </p>
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Database</th>
              <th>Namespace</th>
              <th>Owner</th>
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
                <td className="mono small">{d.namespace}</td>
                <td className="mono small">{d.owner || "—"}</td>
                <td>
                  {d.ready ? (
                    <span className="status on">ready</span>
                  ) : (
                    <span className="status off" title={d.message}>
                      not ready
                    </span>
                  )}
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
