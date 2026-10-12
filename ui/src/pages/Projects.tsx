import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, type Project, type ProjectInput } from "../api";
import { EmptyState, ErrorBanner, PageHeader, formatTime } from "../components/ui";

function parseSchemas(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function ProjectForm({ editing, onDone }: { editing: Project | null; onDone: () => void }) {
  const queryClient = useQueryClient();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const oracle = (connections.data ?? []).filter((c) => c.kind === "oracle");
  const postgres = (connections.data ?? []).filter((c) => c.kind === "postgres");

  const [name, setName] = useState(editing?.name ?? "");
  const [description, setDescription] = useState(editing?.description ?? "");
  const [oracleId, setOracleId] = useState(editing?.oracle_connection_id ?? "");
  const [postgresId, setPostgresId] = useState(editing?.postgres_connection_id ?? "");
  const [schemas, setSchemas] = useState((editing?.schemas ?? []).join(", "));
  const [targetSchemas, setTargetSchemas] = useState(
    ((editing?.settings?.target_schemas as string[] | undefined) ?? []).join(", "),
  );

  const save = useMutation({
    mutationFn: () => {
      const body: ProjectInput = {
        name,
        description: description || null,
        oracle_connection_id: oracleId,
        postgres_connection_id: postgresId || null,
        schemas: parseSchemas(schemas),
        settings: { ...(editing?.settings ?? {}), target_schemas: parseSchemas(targetSchemas) },
      };
      return editing ? api.updateProject(editing.id, body) : api.createProject(body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      onDone();
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };

  if (connections.isSuccess && oracle.length === 0) {
    return (
      <div className="card">
        <p>A project needs an Oracle connection. <Link to="/connections">Add one first</Link>.</p>
      </div>
    );
  }

  return (
    <form className="card form" onSubmit={submit}>
      <h2>{editing ? `Edit ${editing.name}` : "New project"}</h2>
      <div className="grid">
        <label>
          Name
          <input required value={name} onChange={(e) => setName(e.target.value)} placeholder="Billing migration" />
        </label>
        <label>
          Oracle source
          <select required value={oracleId} onChange={(e) => setOracleId(e.target.value)}>
            <option value="" disabled>Choose…</option>
            {oracle.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <label>
          PostgreSQL target <span className="muted">(needed for comparison)</span>
          <select value={postgresId} onChange={(e) => setPostgresId(e.target.value)}>
            <option value="">None yet</option>
            {postgres.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <label>
          Oracle schemas in scope
          <input value={schemas} onChange={(e) => setSchemas(e.target.value)} placeholder="BILLING, HR" />
        </label>
        <label>
          PostgreSQL schemas to compare <span className="muted">(optional; default all)</span>
          <input value={targetSchemas} onChange={(e) => setTargetSchemas(e.target.value)} placeholder="sales, billing" />
        </label>
        <label className="span-2">
          Description
          <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
      </div>
      <p className="muted small">
        Oracle schema names are matched as stored in the data dictionary (usually upper case); PostgreSQL schema names as
        created (usually lower case). Listing only the application's PostgreSQL schemas makes comparisons faster.
      </p>
      <ErrorBanner error={save.error} />
      <div className="actions">
        <button type="button" className="btn btn-ghost" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</button>
      </div>
    </form>
  );
}

export default function ProjectsPage() {
  const queryClient = useQueryClient();
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const [editing, setEditing] = useState<Project | null>(null);
  const [showForm, setShowForm] = useState(false);
  const remove = useMutation({
    mutationFn: api.deleteProject,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });

  const connName = (id: string | null) => (id ? connections.data?.find((c) => c.id === id)?.name ?? "—" : "—");

  return (
    <>
      <PageHeader
        title="Projects"
        subtitle="A project ties an Oracle source to a PostgreSQL target and the schemas being migrated."
        actions={!showForm && <button className="btn btn-primary" onClick={() => { setEditing(null); setShowForm(true); }}>New project</button>}
      />
      {showForm && <ProjectForm key={editing?.id ?? "new"} editing={editing} onDone={() => setShowForm(false)} />}
      <ErrorBanner error={projects.error ?? remove.error} />

      {projects.data?.length === 0 && !showForm && <EmptyState>No projects yet.</EmptyState>}

      {!!projects.data?.length && (
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Project</th>
                <th>Oracle source</th>
                <th>PostgreSQL target</th>
                <th>Schemas</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {projects.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    <Link to={`/projects/${p.id}`} className="cell-title">{p.name}</Link>
                    {p.description && <div className="muted small">{p.description}</div>}
                  </td>
                  <td>{connName(p.oracle_connection_id)}</td>
                  <td>{connName(p.postgres_connection_id)}</td>
                  <td className="mono small">{p.schemas.join(", ") || <span className="muted">all visible</span>}</td>
                  <td className="small">{formatTime(p.created_at)}</td>
                  <td className="row-actions">
                    <button className="btn btn-sm btn-ghost" onClick={() => { setEditing(p); setShowForm(true); }}>Edit</button>
                    <button
                      className="btn btn-sm btn-ghost danger"
                      onClick={() => { if (confirm(`Delete project "${p.name}" and its history?`)) remove.mutate(p.id); }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
