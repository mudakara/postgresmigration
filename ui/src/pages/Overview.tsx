import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../api";
import { ErrorBanner, PageHeader } from "../components/ui";

export default function OverviewPage() {
  const system = useQuery({ queryKey: ["system"], queryFn: api.system });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const checks = useQuery({
    queryKey: ["jobs", { type: "ora2pg_check", status: "succeeded" }],
    queryFn: () => api.jobs({ type: "ora2pg_check", status: "succeeded", limit: "1" }),
  });

  const conns = connections.data ?? [];
  const steps = [
    { done: conns.some((c) => c.kind === "oracle"), label: "Add an Oracle connection", to: "/connections" },
    { done: conns.some((c) => c.kind === "oracle" && c.last_test_ok), label: "Test it successfully", to: "/connections" },
    { done: (checks.data ?? []).length > 0, label: "Run an Ora2Pg check (proves the toolchain end to end)", to: "/connections" },
    { done: conns.some((c) => c.kind === "postgres"), label: "Add the migrated PostgreSQL database (for comparison)", to: "/connections" },
    { done: (projects.data ?? []).length > 0, label: "Create a project and choose the schemas in scope", to: "/projects" },
  ];

  return (
    <>
      <PageHeader title="Overview" subtitle="Connect your databases and verify the conversion toolchain." />
      <ErrorBanner error={system.error ?? health.error} />

      <section className="cards">
        <div className="card stat">
          <div className="stat-label">Ora2Pg</div>
          <div className="stat-value">{system.data?.ora2pg_version?.replace("Ora2Pg ", "") ?? "—"}</div>
          <div className="muted small">Instant Client {system.data?.instant_client ?? "—"} · {system.data?.oracle_client_mode} mode</div>
        </div>
        <div className="card stat">
          <div className="stat-label">Target</div>
          <div className="stat-value">PostgreSQL {system.data?.target_pg_version ?? "—"}</div>
          <div className="muted small">Compile gate: plpgsql_check + orafce</div>
        </div>
        <div className="card stat">
          <div className="stat-label">Connections</div>
          <div className="stat-value">{conns.length}</div>
          <div className="muted small">
            {conns.filter((c) => c.kind === "oracle").length} Oracle · {conns.filter((c) => c.kind === "postgres").length} PostgreSQL
          </div>
        </div>
        <div className="card stat">
          <div className="stat-label">Services</div>
          <div className="stat-value">{health.data?.status === "ok" ? "Healthy" : health.data ? "Degraded" : "—"}</div>
          <div className="muted small">
            App DB {health.data?.database ? "✓" : "✗"} · Queue {health.data?.redis ? "✓" : "✗"}
          </div>
        </div>
      </section>

      <section className="card">
        <h2>Getting started</h2>
        <ol className="checklist">
          {steps.map((step) => (
            <li key={step.label} className={step.done ? "done" : ""}>
              <span className="check">{step.done ? "✓" : ""}</span>
              <Link to={step.to}>{step.label}</Link>
            </li>
          ))}
        </ol>
        <p className="muted small">
          Then open the project and run a scan to build the inventory and assessment report.
        </p>
      </section>
    </>
  );
}
