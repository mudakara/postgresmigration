import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, type JobSummary } from "../api";
import { EmptyState, ErrorBanner, JobBadge, PageHeader, formatDuration, formatTime } from "../components/ui";

const isActive = (job: JobSummary) => job.status === "queued" || job.status === "running";

function JobDetail({ jobId }: { jobId: string }) {
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId),
    refetchInterval: (q) => (q.state.data && !isActive(q.state.data) ? false : 1000),
  });

  if (job.error) return <ErrorBanner error={job.error} />;
  if (!job.data) return <div className="card muted">Loading…</div>;
  const j = job.data;

  return (
    <section className="card">
      <div className="detail-header">
        <h2>
          {j.type} <JobBadge status={j.status} />
        </h2>
        <Link to="/jobs" className="btn btn-sm btn-ghost">Close</Link>
      </div>
      <dl className="meta">
        <dt>Created</dt><dd>{formatTime(j.created_at)}</dd>
        <dt>Duration</dt><dd>{formatDuration(j.started_at, j.finished_at)}</dd>
        {j.progress_message && <><dt>Progress</dt><dd>{j.progress_message}</dd></>}
      </dl>
      {isActive(j) && (
        <div className="progress"><div style={{ width: `${Math.round((j.progress ?? 0.05) * 100)}%` }} /></div>
      )}
      {j.error && <div className="banner banner-error mono">{j.error}</div>}
      {j.result && Object.keys(j.result).length > 0 && (
        <>
          <h3>Result</h3>
          <dl className="meta">
            {Object.entries(j.result).map(([key, value]) => (
              <span key={key} style={{ display: "contents" }}>
                <dt>{key.replace(/_/g, " ")}</dt>
                <dd className="mono">{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd>
              </span>
            ))}
          </dl>
        </>
      )}
      {j.log && (
        <>
          <h3>Log</h3>
          <pre className="log">{j.log}</pre>
        </>
      )}
    </section>
  );
}

export default function JobsPage() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.jobs({ limit: "100" }),
    refetchInterval: (q) => (q.state.data?.some(isActive) ? 2000 : 10000),
  });

  return (
    <>
      <PageHeader title="Jobs" subtitle="Background work run by the worker: Ora2Pg runs, scans and comparisons." />
      {jobId && <JobDetail jobId={jobId} />}
      <ErrorBanner error={jobs.error} />
      {jobs.data?.length === 0 && <EmptyState>No jobs yet. Run an Ora2Pg check from the Connections page.</EmptyState>}
      {!!jobs.data?.length && (
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th>Status</th>
                <th>Created</th>
                <th>Duration</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.map((j) => (
                <tr key={j.id} className={`clickable ${j.id === jobId ? "selected" : ""}`} onClick={() => navigate(`/jobs/${j.id}`)}>
                  <td className="cell-title">{j.type}</td>
                  <td><JobBadge status={j.status} /></td>
                  <td className="small">{formatTime(j.created_at)}</td>
                  <td className="small">{formatDuration(j.started_at, j.finished_at)}</td>
                  <td className="small truncate">{j.error ?? j.progress_message ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
