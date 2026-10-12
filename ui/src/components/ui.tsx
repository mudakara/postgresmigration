import type { ReactNode } from "react";

import type { JobStatus } from "../api";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="actions">{actions}</div>}
    </header>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return <div className="banner banner-error">{message}</div>;
}

export function TestBadge({ ok }: { ok: boolean | null }) {
  if (ok === null) return <span className="badge badge-neutral">Not tested</span>;
  return ok ? <span className="badge badge-ok">Connected</span> : <span className="badge badge-bad">Failed</span>;
}

const JOB_BADGE: Record<JobStatus, string> = {
  queued: "badge-neutral",
  running: "badge-info",
  succeeded: "badge-ok",
  failed: "badge-bad",
};

export function JobBadge({ status }: { status: JobStatus }) {
  return <span className={`badge ${JOB_BADGE[status]}`}>{status}</span>;
}

export function KindTag({ kind }: { kind: string }) {
  return <span className={`tag tag-${kind}`}>{kind === "oracle" ? "Oracle" : "PostgreSQL"}</span>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function formatTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatDuration(start: string | null, end: string | null): string {
  if (!start) return "—";
  const ms = (end ? new Date(end) : new Date()).getTime() - new Date(start).getTime();
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}
