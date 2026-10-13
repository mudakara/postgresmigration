import { useQuery } from "@tanstack/react-query";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { api } from "./api";
import ConnectionsPage from "./pages/Connections";
import ExplorerPage from "./pages/Explorer";
import JobsPage from "./pages/Jobs";
import ObjectDetailPage from "./pages/ObjectDetail";
import OverviewPage from "./pages/Overview";
import PairDetailPage from "./pages/PairDetail";
import ProjectDetailPage from "./pages/ProjectDetail";
import ProjectsPage from "./pages/Projects";
import SequenceStatsPage from "./pages/SequenceStats";
import SequencesPage from "./pages/Sequences";
import TableComparePage from "./pages/TableCompare";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/connections", label: "Connections" },
  { to: "/explorer", label: "Schema explorer", end: false },
  { to: "/compare", label: "Table compare", end: false },
  { to: "/sequences", label: "Sequence check", end: false },
  { to: "/sequence-stats", label: "Sequence statistics", end: false },
  { to: "/projects", label: "Projects", end: false },
  { to: "/jobs", label: "Jobs" },
];

export default function App() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 15000 });
  const healthy = health.data?.status === "ok";

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">⇄</span>
          <div>
            <div className="brand-name">Migration Studio</div>
            <div className="brand-sub">Oracle → PostgreSQL</div>
          </div>
        </div>
        <nav>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className="nav-link">
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className={`dot ${health.isLoading ? "" : healthy ? "dot-ok" : "dot-bad"}`} />
          {health.isLoading ? "Checking…" : healthy ? "Backend healthy" : "Backend degraded"}
        </div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
          <Route path="/explorer" element={<ExplorerPage />} />
          <Route path="/explorer/:connectionId" element={<ExplorerPage />} />
          <Route path="/compare" element={<TableComparePage />} />
          <Route path="/sequences" element={<SequencesPage />} />
          <Route path="/sequence-stats" element={<SequenceStatsPage />} />
          <Route path="/sequences/:connectionId" element={<SequencesPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          <Route path="/projects/:projectId/objects/:objectId" element={<ObjectDetailPage />} />
          <Route path="/projects/:projectId/pairs/:pairId" element={<PairDetailPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:jobId" element={<JobsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
