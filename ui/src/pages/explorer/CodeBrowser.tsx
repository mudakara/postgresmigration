import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { catalogApi, type CatalogCodeSummary, type CatalogSnapshot } from "../../api";
import SourceViewer, { type LineMark, type SourceViewerHandle } from "../../components/SourceViewer";
import { EmptyState, ErrorBanner } from "../../components/ui";
import { CodeKind, FoundBy, Operations, matchingLines, setParam } from "./shared";

const KINDS: [string, string][] = [
  ["", "All kinds"],
  ["procedure", "Procedures"],
  ["function", "Functions"],
  ["trigger_function", "Trigger functions"],
  ["package", "Packages"],
  ["trigger", "Triggers"],
];
const ACCESS: [string, string][] = [
  ["", "Any table access"],
  ["writes", "Writes to tables"],
  ["reads_only", "Only reads tables"],
  ["no_tables", "Uses no tables"],
];

function useDebounced(value: string, ms = 250) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

export function CodeList({ snapshot, params, setParams }: {
  snapshot: CatalogSnapshot; params: URLSearchParams; setParams: (p: URLSearchParams) => void;
}) {
  const schema = params.get("schema") ?? "";
  const kind = params.get("ckind") ?? "";
  const access = params.get("access") ?? "";
  const inSource = params.get("src") === "1";
  const invalid = params.get("invalid") === "1";
  const selected = params.get("code");
  const [q, setQ] = useState(params.get("cq") ?? "");
  const debounced = useDebounced(q);
  // Keep the search in the URL so the source view can highlight it and links can be shared.
  useEffect(() => {
    if ((params.get("cq") ?? "") !== debounced) setParams(setParam(params, "cq", debounced || null));
  }, [debounced]); // eslint-disable-line react-hooks/exhaustive-deps

  const query = new URLSearchParams();
  if (schema) query.set("schema", schema);
  if (kind) query.set("kind", kind);
  if (access) query.set("access", access);
  if (debounced) query.set("q", debounced);
  if (debounced && inSource) query.set("in_source", "true");
  if (invalid) query.set("invalid", "true");
  const code = useQuery({
    queryKey: ["catalog-code-list", snapshot.id, query.toString()],
    queryFn: () => catalogApi.codeList(snapshot.id, query),
    placeholderData: (prev) => prev,
  });
  const schemas = (snapshot.summary.schemas ?? []).filter((s) => (s.code ?? 0) > 0);
  const toggle = (key: string, on: boolean) => setParams(setParam(params, key, on ? "1" : null));

  return (
    <>
      <div className="explorer-filters">
        <input type="search" placeholder={inSource ? "Search names and source code…" : "Search procedures, functions, triggers…"}
          value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search code" />
        <label className="checkbox inline small">
          <input type="checkbox" checked={inSource} onChange={(e) => toggle("src", e.target.checked)} />
          Also search inside the source code
        </label>
        <select value={schema} onChange={(e) => setParams(setParam(params, "schema", e.target.value || null))} aria-label="Schema">
          <option value="">All schemas ({schemas.length})</option>
          {schemas.map((s) => <option key={s.schema} value={s.schema}>{s.schema} ({s.code})</option>)}
        </select>
        <div className="explorer-filter-pair">
          <select value={kind} onChange={(e) => setParams(setParam(params, "ckind", e.target.value || null))} aria-label="Kind">
            {KINDS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select value={access} onChange={(e) => setParams(setParam(params, "access", e.target.value || null))} aria-label="Table access">
            {ACCESS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        {snapshot.dictionary && (
          <label className="checkbox inline small">
            <input type="checkbox" checked={invalid} onChange={(e) => toggle("invalid", e.target.checked)} />
            Only invalid (doesn't compile in Oracle)
          </label>
        )}
      </div>
      <div className="explorer-count muted small">
        {code.data ? `${code.data.total.toLocaleString()} ${code.data.total === 1 ? "match" : "matches"}${code.data.total > code.data.items.length ? `, showing ${code.data.items.length}; refine the search` : ""}` : "Loading…"}
      </div>
      <ErrorBanner error={code.error} />
      <div className="explorer-items">
        {code.data?.items.map((c: CatalogCodeSummary) => (
          <button key={c.id} className={`explorer-item ${selected === c.id ? "active" : ""}`}
            onClick={() => setParams(setParam(params, "code", c.id))}>
            <span className="mono explorer-name" title={c.signature ?? c.name}>
              {!schema && <span className="muted">{c.schema_name}.</span>}{c.name}
            </span>
            <span className="explorer-counts">
              {c.status && c.status !== "VALID" && <span className="error-text">invalid</span>}
              <span>{shortKind(c)}</span>
              {c.table_count > 0 && <span title={`Uses ${c.table_count} table(s)`}>⊞{c.table_count}</span>}
            </span>
          </button>
        ))}
      </div>
    </>
  );
}

function shortKind(c: CatalogCodeSummary) {
  if (c.extra?.is_trigger_function) return "trg fn";
  return { procedure: "proc", function: "func", package: "pkg", trigger: "trigger" }[c.kind] ?? c.kind;
}

export function CodeDetail({ codeId, search, onSelectTable }: {
  codeId: string; search: string; onSelectTable: (id: string) => void;
}) {
  const code = useQuery({ queryKey: ["catalog-code", codeId], queryFn: () => catalogApi.code(codeId) });
  const viewer = useRef<SourceViewerHandle>(null);
  const [focus, setFocus] = useState<string | null>(null); // table id whose lines are highlighted
  const c = code.data;
  const hits = useMemo(() => matchingLines(c?.source, search), [c?.source, search]);
  const focused = c?.uses.find((u) => u.table.id === focus);
  const marks = useMemo(() => {
    const m = new Map<number, LineMark>();
    for (const u of c?.uses ?? []) {
      if (focus && u.table.id !== focus) continue;
      for (const line of u.lines) {
        const title = `${u.table.schema_name}.${u.table.name}: ${u.operations.join(", ") || "referenced"}`;
        const mark = m.get(line);
        if (mark) mark.titles.push(title);
        else m.set(line, { severity: focus ? "medium" : "low", titles: [title] });
      }
    }
    for (const line of hits) {
      const mark = m.get(line);
      if (mark) { mark.severity = "high"; mark.titles.push(`Matches "${search}"`); }
      else m.set(line, { severity: "high", titles: [`Matches "${search}"`] });
    }
    return m;
  }, [c, focus, hits, search]);
  const firstHit = hits[0];
  useEffect(() => {
    if (firstHit) setTimeout(() => viewer.current?.scrollToLine(firstHit), 50);
  }, [firstHit, codeId]);

  if (code.error) return <ErrorBanner error={code.error} />;
  if (!c) return <div className="card muted">Loading…</div>;
  const members: string[] = c.extra?.members ?? [];
  const jump = (line: number) => viewer.current?.scrollToLine(line);

  return (
    <section className="card explorer-detail">
      <div className="explorer-title">
        <h2 className="mono"><span className="muted">{c.schema_name}.</span>{c.name}</h2>
        <CodeKind code={c} />
        {c.status && c.status !== "VALID" && <span className="badge badge-bad">{c.status}</span>}
      </div>
      <div className="object-meta small muted">
        {c.language && <span>{c.language}</span>}
        <span className="mono code-signature">{c.signature}</span>
      </div>

      {c.kind === "trigger" && (
        <dl className="meta small">
          <dt>Fires on</dt>
          <dd className="mono">
            {c.table ? <a href="#" onClick={(e) => { e.preventDefault(); onSelectTable(c.table!.id); }}>{c.table.schema_name}.{c.table.name}</a> : (c.extra.base_object_type ?? "—")}
            {c.extra.partition && <span className="muted"> (partition {c.extra.partition})</span>}
          </dd>
          <dt>When</dt><dd>{[c.extra.timing, c.extra.events, c.extra.level && `FOR EACH ${c.extra.level}`].filter(Boolean).join(" ") || "—"}</dd>
          <dt>Enabled</dt><dd>{c.extra.enabled === false ? <span className="badge badge-warn">disabled</span> : "yes"}</dd>
          {c.extra.function && <><dt>Function</dt><dd className="mono">{c.extra.function}</dd></>}
        </dl>
      )}

      <h3>Tables used ({c.uses.length})</h3>
      {c.uses.length === 0 ? (
        <p className="muted small">
          {c.kind === "trigger" && c.extra.function
            ? "The trigger's function holds the table references; open it from the list (kind: trigger functions)."
            : "No table references found."}
        </p>
      ) : (
        <div className="table-scroll">
          <table className="compact">
            <thead><tr><th>Table</th><th>How it is used</th><th>Found by</th><th>Lines</th></tr></thead>
            <tbody>
              {c.uses.map((u) => (
                <tr key={u.table.id} className={focus === u.table.id ? "selected" : ""}>
                  <td className="mono">
                    <a href="#" onClick={(e) => { e.preventDefault(); onSelectTable(u.table.id); }}>{u.table.schema_name}.{u.table.name}</a>
                    {u.subprograms.length > 0 && <div className="muted small">in {u.subprograms.join(", ")}</div>}
                  </td>
                  <td><Operations ops={u.operations} /></td>
                  <td><FoundBy usage={u} /></td>
                  <td>
                    {u.lines.length > 0 && (
                      <button className="btn btn-sm" onClick={() => { setFocus(focus === u.table.id ? null : u.table.id); jump(u.lines[0]); }}>
                        {focus === u.table.id ? "Show all" : `Highlight (${u.lines.length})`}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="source-head">
        <h3>Source</h3>
        <div className="line-chips small">
          {hits.length > 0 && <span className="muted">{hits.length} line(s) match “{search}”:</span>}
          {hits.slice(0, 30).map((l) => <button key={l} className="chip chip-btn" onClick={() => jump(l)}>{l}</button>)}
          {focused && <span className="muted">Highlighting {focused.table.name}</span>}
        </div>
      </div>
      {c.source ? <SourceViewer ref={viewer} source={c.source} marks={marks} maxHeight="70vh" /> : <EmptyState>Source is not visible to this account.</EmptyState>}
      {members.length > 0 && (
        <details className="small">
          <summary>{members.length} package member(s)</summary>
          <ul className="dep-list mono">{members.map((m) => <li key={m}>{m}</li>)}</ul>
        </details>
      )}
    </section>
  );
}
