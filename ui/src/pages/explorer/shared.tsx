import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import { catalogApi, type CatalogCodeSummary, type CatalogUsageInfo } from "../../api";
import SourceViewer, { type LineMark, type SourceViewerHandle } from "../../components/SourceViewer";
import { ErrorBanner } from "../../components/ui";

export const WRITES = new Set(["insert", "update", "delete", "merge", "truncate", "ddl"]);
export const FOUND_BY: Record<string, { label: string; title: string }> = {
  dictionary: { label: "Oracle dependency", title: "Recorded in Oracle's dependency views (static SQL)" },
  code: { label: "source", title: "Found by reading the source code" },
  dynamic_sql: { label: "dynamic SQL", title: "Named inside a string: dynamic SQL, which no dependency view tracks" },
};

export function setParam(params: URLSearchParams, key: string, value: string | null) {
  const next = new URLSearchParams(params);
  if (value) next.set(key, value);
  else next.delete(key);
  return next;
}

export function Operations({ ops }: { ops: string[] }) {
  if (!ops.length) return <span className="muted small" title="The dependency is recorded, but how it is used was not found in the source">referenced</span>;
  return (
    <span className="chips">
      {ops.map((op) => (
        <span key={op} className={`op ${WRITES.has(op) ? "op-write" : op === "select" ? "op-read" : ""}`}>
          {op === "type" ? "%ROWTYPE" : op.toUpperCase()}
        </span>
      ))}
    </span>
  );
}

export function FoundBy({ usage }: { usage: CatalogUsageInfo }) {
  return (
    <span className="chips">
      {usage.found_by.map((f) => (
        <span key={f} className={`badge plain ${f === "dynamic_sql" ? "badge-warn" : "badge-neutral"}`} title={FOUND_BY[f]?.title}>
          {FOUND_BY[f]?.label ?? f}
        </span>
      ))}
    </span>
  );
}

export function CodeKind({ code }: { code: CatalogCodeSummary }) {
  const kind = code.extra?.is_trigger_function ? "trigger function" : code.kind;
  return <span className={`tag tag-code tag-${code.kind}`}>{kind}</span>;
}

// --- code source panel --------------------------------------------------------------

export function CodePanel({ codeId, tableId, onSelectTable, onOpenCode }: {
  codeId: string; tableId: string; onSelectTable: (id: string) => void; onOpenCode?: (id: string) => void;
}) {
  const code = useQuery({ queryKey: ["catalog-code", codeId], queryFn: () => catalogApi.code(codeId) });
  const viewer = useRef<SourceViewerHandle>(null);
  const here = code.data?.uses.find((u) => u.table.id === tableId);
  const marks = useMemo(() => {
    const m = new Map<number, LineMark>();
    for (const line of here?.lines ?? []) m.set(line, { severity: "medium", titles: [`Uses ${here!.table.schema_name}.${here!.table.name}`] });
    return m;
  }, [here]);
  const firstLine = here?.lines[0];
  useEffect(() => {
    if (firstLine) setTimeout(() => viewer.current?.scrollToLine(firstLine), 50);
  }, [firstLine]);

  if (code.error) return <ErrorBanner error={code.error} />;
  if (!code.data) return <div className="muted small">Loading source…</div>;
  const c = code.data;
  const others = c.uses.filter((u) => u.table.id !== tableId);
  const members: string[] = c.extra?.members ?? [];
  return (
    <div className="code-panel">
      <div className="code-panel-meta">
        <span className="mono small">{c.signature}</span>
        {c.status && c.status !== "VALID" && <span className="badge badge-bad">{c.status}</span>}
        {c.language && <span className="muted small">{c.language}</span>}
        {onOpenCode && (
          <button className="btn btn-sm btn-ghost" onClick={() => onOpenCode(c.id)}>Open in code view →</button>
        )}
      </div>
      {here && here.lines.length > 0 && (
        <div className="line-chips small">
          <span className="muted">Lines that use this table:</span>
          {here.lines.map((l) => (
            <button key={l} className="chip chip-btn" onClick={() => viewer.current?.scrollToLine(l)}>{l}</button>
          ))}
        </div>
      )}
      {c.source ? (
        <SourceViewer ref={viewer} source={c.source} marks={marks} maxHeight="460px" />
      ) : (
        <p className="muted small">Source is not visible to this account.</p>
      )}
      {others.length > 0 && (
        <div className="line-chips small">
          <span className="muted">Also uses:</span>
          {others.map((u) => (
            <button key={u.table.id} className="chip chip-btn" onClick={() => onSelectTable(u.table.id)} title={u.operations.join(", ")}>
              {u.table.schema_name}.{u.table.name}
            </button>
          ))}
        </div>
      )}
      {members.length > 0 && (
        <details className="small">
          <summary>{members.length} package member(s)</summary>
          <ul className="dep-list mono">{members.map((m) => <li key={m}>{m}</li>)}</ul>
        </details>
      )}
    </div>
  );
}

/** Lines of `source` containing `needle` (case-insensitive), 1-based. */
export function matchingLines(source: string | null | undefined, needle: string): number[] {
  const q = needle.trim().toLowerCase();
  if (!source || !q) return [];
  const out: number[] = [];
  source.split("\n").forEach((line, i) => {
    if (line.toLowerCase().includes(q)) out.push(i + 1);
  });
  return out;
}
