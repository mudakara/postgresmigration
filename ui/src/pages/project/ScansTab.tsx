import { Link } from "react-router-dom";

import type { Scan } from "../../api";
import { JobBadge, formatDuration, formatTime } from "../../components/ui";

function ora2pgCell(scan: Scan) {
  if (!scan.include_ora2pg_cost) return <span className="muted">skipped</span>;
  if (!scan.ora2pg_reports.length) return "—";
  const failed = scan.ora2pg_reports.filter((r) => !r.ok).length;
  return failed ? <span className="error-text">{failed} schema(s) failed</span> : "✓";
}

export default function ScansTab({ scans }: { scans: Scan[] }) {
  return (
    <div className="card table-card">
      <table>
        <thead>
          <tr>
            <th>Started</th>
            <th>Status</th>
            <th>Duration</th>
            <th className="num">Objects</th>
            <th>Changes</th>
            <th className="num">Unsafe</th>
            <th>Ora2Pg</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>
          {scans.map((s) => (
            <tr key={s.id}>
              <td className="small">{formatTime(s.started_at ?? s.created_at)}</td>
              <td><JobBadge status={s.status} /></td>
              <td className="small">{formatDuration(s.started_at, s.finished_at)}</td>
              <td className="num">{s.summary.objects ?? "—"}</td>
              <td className="small mono">
                {s.status === "succeeded" ? `+${s.summary.added} ~${s.summary.changed} −${s.summary.removed}` : "—"}
              </td>
              <td className="num">{s.summary.unsafe ?? "—"}</td>
              <td className="small">{ora2pgCell(s)}</td>
              <td className="small">
                {s.error && <span className="error-text">{s.error}</span>}
                {s.warnings.length > 0 && <span title={s.warnings.join("\n")}>{s.warnings.length} warning(s)</span>}{" "}
                {s.job_id && <Link to={`/jobs/${s.job_id}`}>Log</Link>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small card-pad">Changes: + new objects, ~ objects whose Oracle source changed, − objects no longer in Oracle.</p>
    </div>
  );
}
