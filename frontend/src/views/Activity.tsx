import { useEffect, useState } from "react";
import { api, type Environment, type Job } from "../api";
import { useSession } from "../App";
import { Confirm } from "../components/Dialog";
import { Loading, message, StateBadge, useLoad } from "../components/ui";
import { bytes, duration, when } from "../format";

function what(job: Job, env: string): string {
  if (job.action === "snapshot") return "Snapshot";
  if (job.action === "script") return job.mode === "check" ? "Check a script" : job.snapshot ? "Run a script, after a snapshot" : "Run a script";
  const from = job.source_environment && job.source_environment !== env ? `Clone from ${job.source_environment}/${job.source_database}` : "Restore";
  return job.into === "copy" ? `${from} into ${job.target_database}` : from;
}

export function Activity({ env }: { env: Environment }) {
  const { notify } = useSession();
  const { data, error, reload } = useLoad(() => api.jobs(env.name), [env.name]);
  const [declining, setDeclining] = useState<Job | null>(null);
  const [busy, setBusy] = useState("");

  // While something runs, look again every few seconds.
  const moving = data?.some((j) => j.state === "starting" || j.state === "running");
  useEffect(() => {
    if (!moving) return;
    const timer = window.setInterval(reload, 4000);
    return () => window.clearInterval(timer);
  }, [moving, reload]);

  async function act(job: Job, run: () => Promise<unknown>, done: string) {
    setBusy(job.name);
    try {
      await run();
      notify(done);
      setDeclining(null);
      reload();
    } catch (err) {
      notify(message(err), "error");
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Activity in {env.name}</h1>
        <p className="muted">Snapshots, restores and scripts of the last week. {env.production && "A restore or a script here starts once a second person approves it."}</p>
        <div className="actions">
          <button type="button" className="button small" onClick={reload}>
            Refresh
          </button>
        </div>
      </div>
      {!data ? (
        <Loading error={error} />
      ) : data.length === 0 ? (
        <div className="empty">
          <p>Nothing has run here this week.</p>
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Asked</th>
              <th>What</th>
              <th>Database</th>
              <th>State</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {data.map((j) => (
              <tr key={j.name}>
                <td className="nowrap" title={j.name}>
                  {when(j.requested_at)}
                </td>
                <td>
                  {what(j, env.name)}
                  {(j.reason || j.note) && <div className="muted small">{j.reason || j.note}</div>}
                  <div className="muted small">
                    by {j.requested_by}
                    {j.approved_by && `, approved by ${j.approved_by}`}
                  </div>
                  {j.script_text && (
                    // Open while it waits: whoever approves it reads what will run.
                    <details className="script-details" open={j.state === "waiting"}>
                      <summary>The script{j.script_sha256 && <span className="muted"> · sha256 {j.script_sha256.slice(0, 12)}</span>}</summary>
                      <pre className="output">{j.script_text}</pre>
                    </details>
                  )}
                </td>
                <td className="mono">{j.database}</td>
                <td>
                  <StateBadge state={j.state} />
                  {(j.can_approve || j.can_decline) && (
                    <div className="row-actions">
                      {j.can_approve && (
                        <button type="button" className="button small primary" disabled={busy === j.name} onClick={() => void act(j, () => api.approve(env.name, j.name), j.action === "script" ? "Approved; the script starts now." : "Approved; the restore starts now.")}>
                          Approve
                        </button>
                      )}
                      {j.can_decline && (
                        <button type="button" className="button small" disabled={busy === j.name} onClick={() => setDeclining(j)}>
                          {j.can_approve ? "Decline" : "Withdraw"}
                        </button>
                      )}
                    </div>
                  )}
                </td>
                <td className="small">
                  {j.result.result === "succeeded" ? (
                    j.action === "script" ? (
                      <>
                        {j.result.mode === "check" ? "Checked, rolled back" : "Committed"} in {duration(j.result.seconds)}
                        {j.result.bytes ? `, after a snapshot of ${bytes(j.result.bytes)}` : ""}
                      </>
                    ) : (
                      <>
                        {bytes(j.result.bytes)} in {duration(j.result.seconds)}
                      </>
                    )
                  ) : (
                    j.result.reason && <span className="danger-text">{j.result.reason}</span>
                  )}
                  {j.result.output && (
                    <details className="script-details" open={j.result.result === "failed"}>
                      <summary>Output</summary>
                      <pre className="output">{j.result.output}</pre>
                    </details>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {declining && (
        <Confirm
          title={declining.can_approve ? `Decline this ${declining.action}?` : "Withdraw your request?"}
          action={declining.can_approve ? "Decline" : "Withdraw"}
          danger
          busy={busy === declining.name}
          onConfirm={() => void act(declining, () => api.decline(env.name, declining.name, ""), "The request is gone.")}
          onClose={() => setDeclining(null)}
        >
          <p>
            {what(declining, env.name)} of <span className="mono">{declining.database}</span>, asked by {declining.requested_by}, will not run.
          </p>
        </Confirm>
      )}
    </>
  );
}
