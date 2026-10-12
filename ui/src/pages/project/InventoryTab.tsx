import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { inventoryApi } from "../../api";
import { CATEGORY_LABEL, CATEGORY_ORDER, TIER_LABEL, TIER_ORDER, TierChip, UnsafeBadge } from "../../components/inventory";
import { EmptyState, ErrorBanner, formatTime } from "../../components/ui";

const PAGE_SIZE = 50;
const FILTER_KEYS = ["type", "owner", "complexity", "finding", "category", "unsafe", "invalid", "q"];
const SORTABLE: { key: string; label: string; numeric?: boolean }[] = [
  { key: "name", label: "Object" },
  { key: "type", label: "Type" },
  { key: "lines", label: "Lines", numeric: true },
  { key: "effort", label: "Effort", numeric: true },
  { key: "findings", label: "Findings", numeric: true },
  { key: "changed", label: "Last DDL" },
];

export default function InventoryTab({ projectId }: { projectId: string }) {
  const [params, setParams] = useSearchParams();
  const report = useQuery({ queryKey: ["report", projectId], queryFn: () => inventoryApi.report(projectId) });
  const [search, setSearch] = useState(params.get("q") ?? "");

  const sort = params.get("sort") ?? "name";
  const order = params.get("order") ?? (sort === "name" || sort === "type" ? "asc" : "desc");
  const page = Number(params.get("page") ?? "0");

  const apiParams = new URLSearchParams();
  for (const key of FILTER_KEYS) for (const v of params.getAll(key)) apiParams.append(key, v);
  apiParams.set("sort", sort);
  apiParams.set("order", order);
  apiParams.set("limit", String(PAGE_SIZE));
  apiParams.set("offset", String(page * PAGE_SIZE));
  const filterOnly = new URLSearchParams();
  for (const key of FILTER_KEYS) for (const v of params.getAll(key)) filterOnly.append(key, v);

  const objects = useQuery({
    queryKey: ["objects", projectId, apiParams.toString()],
    queryFn: () => inventoryApi.objects(projectId, apiParams),
    placeholderData: keepPreviousData,
  });

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
    }
    if (!("page" in changes)) next.delete("page");
    next.set("tab", "inventory");
    setParams(next, { replace: true });
  };

  // Debounce the search box into the URL.
  useEffect(() => {
    const handle = setTimeout(() => {
      if (search !== (params.get("q") ?? "")) update({ q: search });
    }, 300);
    return () => clearTimeout(handle);
  }, [search]);

  const toggleSort = (key: string) => {
    const defaultOrder = key === "name" || key === "type" ? "asc" : "desc";
    const nextOrder = sort === key ? (order === "asc" ? "desc" : "asc") : defaultOrder;
    update({ sort: key, order: nextOrder });
  };

  const types = report.data?.by_type.map((t) => t.object_type) ?? [];
  const owners = report.data?.by_owner.map((o) => o.owner) ?? [];
  const findings = report.data?.findings ?? [];
  const total = objects.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const activeFilters = FILTER_KEYS.some((k) => params.has(k));

  return (
    <>
      <div className="filters card">
        <input type="search" placeholder="Search name or schema…" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search objects" />
        <select value={params.get("type") ?? ""} onChange={(e) => update({ type: e.target.value })} aria-label="Object type">
          <option value="">All types</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        {owners.length > 1 && (
          <select value={params.get("owner") ?? ""} onChange={(e) => update({ owner: e.target.value })} aria-label="Schema">
            <option value="">All schemas</option>
            {owners.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        )}
        <select value={params.get("complexity") ?? ""} onChange={(e) => update({ complexity: e.target.value })} aria-label="Complexity">
          <option value="">Any complexity</option>
          {TIER_ORDER.map((t) => <option key={t} value={t}>{TIER_LABEL[t]}</option>)}
        </select>
        <select value={params.get("category") ?? ""} onChange={(e) => update({ category: e.target.value })} aria-label="Finding category">
          <option value="">Any category</option>
          {CATEGORY_ORDER.map((c) => <option key={c} value={c}>{CATEGORY_LABEL[c]}</option>)}
        </select>
        <select value={params.get("finding") ?? ""} onChange={(e) => update({ finding: e.target.value })} aria-label="Finding">
          <option value="">Any finding</option>
          {findings.map((f) => <option key={f.code} value={f.code}>{f.title} ({f.objects})</option>)}
        </select>
        <label className="checkbox inline">
          <input type="checkbox" checked={params.get("unsafe") === "true"} onChange={(e) => update({ unsafe: e.target.checked ? "true" : null })} />
          Unsafe only
        </label>
        <div className="filters-end">
          {activeFilters && (
            <button className="btn btn-sm btn-ghost" onClick={() => { setSearch(""); setParams(new URLSearchParams({ tab: "inventory" }), { replace: true }); }}>
              Clear filters
            </button>
          )}
          <a className="btn btn-sm" href={inventoryApi.csvUrl(projectId, filterOnly)} download>Export CSV</a>
        </div>
      </div>

      <ErrorBanner error={objects.error} />
      {objects.data && objects.data.total === 0 && <EmptyState>No objects match these filters.</EmptyState>}

      {!!objects.data?.items.length && (
        <div className="card table-card">
          <table className="inventory-table">
            <thead>
              <tr>
                {SORTABLE.map((col) => (
                  <th key={col.key} className={col.numeric ? "num" : ""} aria-sort={sort === col.key ? (order === "asc" ? "ascending" : "descending") : "none"}>
                    <button className="th-sort" onClick={() => toggleSort(col.key)}>
                      {col.label} {sort === col.key ? (order === "asc" ? "↑" : "↓") : ""}
                    </button>
                  </th>
                ))}
                <th>Complexity</th>
              </tr>
            </thead>
            <tbody>
              {objects.data.items.map((o) => (
                <tr key={o.id}>
                  <td>
                    <Link to={`/projects/${projectId}/objects/${o.id}`} className="obj-link">
                      <span className="muted">{o.owner}.</span>{o.name}
                    </Link>
                    <div className="row-flags">
                      {o.unsafe_to_execute && <UnsafeBadge compact />}
                      {o.status !== "VALID" && <span className="badge badge-bad">✗ Invalid</span>}
                      {o.wrapped && <span className="badge badge-bad">✗ Wrapped</span>}
                    </div>
                  </td>
                  <td className="small">{o.object_type}</td>
                  <td className="num">{o.line_count.toLocaleString()}</td>
                  <td className="num" title={o.ora2pg_cost != null ? "Ora2Pg cost estimate" : "Heuristic estimate"}>
                    {o.effort}{o.ora2pg_cost == null && <span className="muted">*</span>}
                  </td>
                  <td className="num">
                    <span title={o.finding_codes.join(", ")}>{o.finding_codes.length || "—"}</span>
                  </td>
                  <td className="small">{formatTime(o.oracle_last_ddl)}</td>
                  <td><TierChip tier={o.complexity} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="pager">
            <span className="muted small">
              {(page * PAGE_SIZE + 1).toLocaleString()}–{Math.min(total, (page + 1) * PAGE_SIZE).toLocaleString()} of {total.toLocaleString()}
              {objects.data.items.some((o) => o.ora2pg_cost == null) && " · * heuristic effort (no Ora2Pg cost for this object)"}
            </span>
            <div className="actions">
              <button className="btn btn-sm" disabled={page === 0} onClick={() => update({ page: String(page - 1) })}>Previous</button>
              <span className="small">Page {page + 1} of {pages}</span>
              <button className="btn btn-sm" disabled={page + 1 >= pages} onClick={() => update({ page: String(page + 1) })}>Next</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
