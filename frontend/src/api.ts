// The backend's JSON API. Same origin, the session cookie rides along, and every write carries
// the session's CSRF token. Who may see and do what is decided by the server; these types only
// say what comes back.

export type Environment = { name: string; production: boolean; runs_jobs: boolean; clone_from: string[]; downloads: boolean };

export type Me = {
  name: string;
  upn: string;
  csrf: string;
  expires: number;
  version: string;
  release_notes: string | null;
  admin: boolean;
  auditor: boolean;
  teams: { id: string; name: string }[];
  environments: Environment[];
};

export type Database = {
  name: string;
  owner: string;
  ready: boolean | null;
  message: string;
  teams: string[];
  workloads: string[];
  copy: { name: string; ready: boolean } | null;
  can_act: boolean;
};

export type Snapshot = { id: string; created: string; size: number; created_by: string; note: string; sha256: string };

export type JobState = "waiting" | "starting" | "running" | "succeeded" | "failed" | "expired";

export type Job = {
  name: string;
  action: "snapshot" | "restore";
  database: string;
  state: JobState;
  requested_by: string;
  requested_at: string;
  approved_by: string;
  approved_at: string;
  reason: string;
  note: string;
  snapshot: string;
  source_environment: string;
  source_database: string;
  into: "" | "database" | "copy";
  target_database: string;
  approval: string;
  started: string;
  finished: string;
  result: { result?: string; bytes?: number; seconds?: number; reason?: string };
  can_approve: boolean;
  can_decline: boolean;
};

export type AuditEntry = { at: string; environment: string; actor: string; action: string; database?: string; job?: string; snapshot?: string; target?: string; detail?: string };

export type RestoreRequest = { source_environment: string; source_database: string; snapshot: string; into: "database" | "copy"; reason: string };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

const MESSAGES: Record<string, string> = {
  signed_out: "Your session has ended. Sign in again.",
  csrf: "This page is out of date. Reload it and try again.",
  not_allowed: "You may not do that with this database.",
  invalid_request: "Some fields are not valid.",
};

let csrf = "";
let onSignedOut: () => void = () => {};

export function configure(token: string, signedOut: () => void) {
  csrf = token;
  onSignedOut = signedOut;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new ApiError(0, "network", "The server could not be reached.");
  }
  const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = String(data.error ?? response.status);
    if (response.status === 401) onSignedOut();
    throw new ApiError(response.status, code, data.message ?? MESSAGES[code] ?? `Request failed (${response.status}).`);
  }
  return data as T;
}

const env = (name: string) => `/api/environments/${encodeURIComponent(name)}`;
const db = (name: string, database: string) => `${env(name)}/databases/${encodeURIComponent(database)}`;

export const api = {
  me: () => request<Me>("GET", "/api/me"),
  logout: () => request<unknown>("POST", "/auth/logout"),
  databases: (e: string) => request<{ databases: Database[] }>("GET", `${env(e)}/databases`).then((r) => r.databases),
  snapshots: (e: string, d: string) => request<{ snapshots: Snapshot[] }>("GET", `${db(e, d)}/snapshots`).then((r) => r.snapshots),
  snapshot: (e: string, d: string, note: string) => request<Job>("POST", `${db(e, d)}/snapshots`, { note }),
  restore: (e: string, d: string, body: RestoreRequest) => request<Job>("POST", `${db(e, d)}/restore`, body),
  dropCopy: (e: string, d: string) => request<unknown>("DELETE", `${db(e, d)}/copy`),
  downloadUrl: (e: string, d: string, id: string) => `${db(e, d)}/snapshots/${encodeURIComponent(id)}/download`,
  jobs: (e: string) => request<{ jobs: Job[] }>("GET", `${env(e)}/jobs`).then((r) => r.jobs),
  approve: (e: string, name: string) => request<Job>("POST", `${env(e)}/jobs/${encodeURIComponent(name)}/approve`),
  decline: (e: string, name: string, reason: string) => request<unknown>("POST", `${env(e)}/jobs/${encodeURIComponent(name)}/decline`, { reason }),
  audit: (e: string) => request<{ entries: AuditEntry[] }>("GET", `${env(e)}/audit`).then((r) => r.entries),
};
