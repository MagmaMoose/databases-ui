import { api, type Environment } from "../api";
import { Loading, useLoad } from "../components/ui";

export function Users({ env }: { env: Environment }) {
  const { data, error } = useLoad(() => api.users(env.name), [env.name]);

  return (
    <>
      <div className="page-head">
        <h1>Database users in {env.name}</h1>
        <p className="muted">The roles that own the databases you see. Each one's password is in its Secret, next to the database cluster.</p>
      </div>
      {!data ? (
        <Loading error={error} />
      ) : data.length === 0 ? (
        <div className="empty">
          <p>No database users in namespaces you can see.</p>
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Role</th>
              <th>Owns</th>
              <th>Namespace</th>
              <th>Password Secret</th>
            </tr>
          </thead>
          <tbody>
            {data.map((u) => (
              <tr key={u.name}>
                <td className="mono">
                  {u.name}
                  {!u.login && <span className="badge">no login</span>}
                </td>
                <td className="mono small">
                  {u.databases.map((d) => (
                    <div key={d}>
                      <a href={`#/${env.name}/databases/${encodeURIComponent(d)}`}>{d}</a>
                    </div>
                  ))}
                </td>
                <td className="mono small">{u.namespaces.join(", ")}</td>
                <td className="mono small">{u.secret || <span className="muted">{u.managed ? "none" : "not a managed role"}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
