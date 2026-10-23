import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ApiError, type Job } from "../api";

// A loader for one view's data: the value, its error, and a reload.
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: string; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);
  useEffect(() => {
    let live = true;
    setError("");
    run().then(
      (value) => live && setData(value),
      (err) => live && setError(err instanceof ApiError ? err.message : String(err)),
    );
    return () => {
      live = false;
    };
  }, [run, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, reload };
}

export function Loading({ error, children }: { error: string; children?: ReactNode }) {
  if (error)
    return (
      <p className="notice error" role="alert">
        {error}
      </p>
    );
  return <p className="muted">{children ?? "Loading…"}</p>;
}

const STATES: Record<Job["state"], [string, string]> = {
  waiting: ["Waiting for approval", "state-open"],
  starting: ["Starting", "state-open"],
  running: ["Running", "state-open"],
  succeeded: ["Done", "state-granted"],
  failed: ["Failed", "state-declined"],
  expired: ["Expired", "state-declined"],
};

export function StateBadge({ state }: { state: Job["state"] }) {
  const [label, cls] = STATES[state];
  return <span className={`badge ${cls}`}>{label}</span>;
}

export function message(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}
