import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api, inventoryApi, type Scan } from "../api";
import { EmptyState, ErrorBanner, formatTime } from "../components/ui";
import ComparisonTab from "./project/ComparisonTab";
import InventoryTab from "./project/InventoryTab";
import ReportTab from "./project/ReportTab";
import ScansTab from "./project/ScansTab";

const TABS = [
  { key: "report", label: "Report" },
  { key: "inventory", label: "Inventory" },
  { key: "comparison", label: "Comparison with PostgreSQL" },
  { key: "scans", label: "Scan history" },
] as const;

const isActive = (scan?: Scan | null) => !!scan && (scan.status === "queued" || scan.status === "running");

function ScanProgress({ scan, onDone }: { scan: Scan; onDone: () => void }) {
  const job = useQuery({
    queryKey: ["job", scan.job_id],
    queryFn: () => api.job(scan.job_id!),
    enabled: !!scan.job_id,
    refetchInterval: 1500,
  });
  const status = job.data?.status;
  useEffect(() => {
    if (status === "succeeded" || status === "failed") onDone();
  }, [status, onDone]);

  return (
    <div className="card scan-progress">
      <div className="scan-progress-head">
        <strong>Scanning Oracle…</strong>
        <span className="muted small">{job.data?.progress_message ?? "Queued"}</span>
      </div>
      <div className="progress"><div style={{ width: `${Math.round((job.data?.progress ?? 0.02) * 100)}%` }} /></div>
    </div>
  );
}

export default function ProjectDetailPage() {
  const { projectId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as (typeof TABS)[number]["key"]) ?? "report";
  const queryClient = useQueryClient();
  const [withCost, setWithCost] = useState(true);

  const project = useQuery({ queryKey: ["project", projectId], queryFn: () => inventoryApi.project(projectId) });
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const scans = useQuery({
    queryKey: ["scans", projectId],
    queryFn: () => inventoryApi.scans(projectId),
    refetchInterval: (q) => (isActive(q.state.data?.[0]) ? 3000 : false),
  });
  const latest = scans.data?.[0];
  const hasInventory = !!scans.data?.some((s) => s.status === "succeeded");

  const refresh = () => {
    for (const key of [["scans", projectId], ["report", projectId], ["objects", projectId]]) {
      queryClient.invalidateQueries({ queryKey: key });
    }
  };

  const start = useMutation({
    mutationFn: () => inventoryApi.startScan(projectId, withCost),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["scans", projectId] }),
  });

  const setTab = (key: string) => {
    const next = new URLSearchParams();
    next.set("tab", key);
    setParams(next);
  };

  const oracle = connections.data?.find((c) => c.id === project.data?.oracle_connection_id);

  return (
    <>
      <div className="breadcrumb small"><Link to="/projects">Projects</Link> › {project.data?.name ?? "…"}</div>
      <header className="page-header">
        <div>
          <h1>{project.data?.name ?? "Project"}</h1>
          <p className="muted">
            Oracle source <strong>{oracle?.name ?? "—"}</strong> · schemas{" "}
            <span className="mono">{project.data?.schemas.length ? project.data.schemas.join(", ") : "all visible"}</span>
            {latest?.finished_at && <> · last scan {formatTime(latest.finished_at)}</>}
          </p>
        </div>
        <div className="actions scan-actions">
          <label className="checkbox inline" title="Runs `ora2pg -t SHOW_REPORT --estimate_cost` per schema. Slower on large schemas.">
            <input type="checkbox" checked={withCost} onChange={(e) => setWithCost(e.target.checked)} />
            Ora2Pg cost estimate
          </label>
          <button className="btn btn-primary" disabled={start.isPending || isActive(latest)} onClick={() => start.mutate()}>
            {isActive(latest) ? "Scan running…" : hasInventory ? "Re-scan" : "Run scan"}
          </button>
        </div>
      </header>

      <ErrorBanner error={project.error ?? start.error ?? scans.error} />
      {latest && isActive(latest) && <ScanProgress key={latest.id} scan={latest} onDone={refresh} />}
      {latest?.status === "failed" && (
        <div className="banner banner-error">
          Last scan failed: {latest.error} {latest.job_id && <Link to={`/jobs/${latest.job_id}`}>View log</Link>}
        </div>
      )}

      {scans.isSuccess && !hasInventory && !isActive(latest) && (
        <EmptyState>
          No inventory yet. <strong>Run scan</strong> to read every procedure, function, package, trigger, type and view
          {project.data?.schemas.length ? ` in ${project.data.schemas.join(", ")}` : " in the visible schemas"} and build the
          assessment report.
        </EmptyState>
      )}

      {hasInventory && (
        <>
          <nav className="tabs" role="tablist">
            {TABS.map((t) => (
              <button key={t.key} role="tab" aria-selected={tab === t.key} className={`tab ${tab === t.key ? "active" : ""}`} onClick={() => setTab(t.key)}>
                {t.label}
              </button>
            ))}
          </nav>
          {tab === "report" && <ReportTab projectId={projectId} />}
          {tab === "inventory" && <InventoryTab projectId={projectId} />}
          {tab === "comparison" && project.data && <ComparisonTab project={project.data} />}
          {tab === "scans" && <ScansTab scans={scans.data ?? []} />}
        </>
      )}
    </>
  );
}
