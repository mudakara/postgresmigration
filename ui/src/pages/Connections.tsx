import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { api, type Connection, type ConnectionInput, type DbKind, type TestResult } from "../api";
import { EmptyState, ErrorBanner, KindTag, PageHeader, TestBadge, formatTime } from "../components/ui";

const DEFAULT_PORT: Record<DbKind, number> = { oracle: 1521, postgres: 5432 };
const SSL_MODES = ["", "disable", "allow", "prefer", "require", "verify-ca", "verify-full"];

function emptyForm(kind: DbKind = "oracle"): ConnectionInput {
  return { name: "", kind, host: "", port: DEFAULT_PORT[kind], database: "", username: "", password: "", options: {} };
}

function cleanOptions(form: ConnectionInput) {
  return form.kind === "oracle"
    ? { use_sid: !!form.options.use_sid }
    : form.options.sslmode
      ? { sslmode: form.options.sslmode }
      : {};
}

function CapabilitySummary({ conn }: { conn: Connection }) {
  const caps = conn.capabilities as Record<string, any>;
  if (!conn.server_version) return null;
  if (conn.kind === "oracle") {
    return (
      <span className="muted small">
        {conn.server_version} · {caps.container} ·{" "}
        {caps.dba_views ? "DBA_* views ✓" : "no DBA_* access (ALL_* fallback)"}
      </span>
    );
  }
  const exts = Object.entries((caps.extensions ?? {}) as Record<string, { available: boolean; installed_version: string | null }>);
  return (
    <span className="muted small">
      {conn.server_version} ·{" "}
      {exts.map(([name, e]) => `${name} ${e.installed_version ? "✓" : e.available ? "(available)" : "✗"}`).join(" · ")}
    </span>
  );
}

function ConnectionForm({ editing, onDone }: { editing: Connection | null; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ConnectionInput>(() =>
    editing
      ? {
          name: editing.name, kind: editing.kind, host: editing.host, port: editing.port,
          database: editing.database, username: editing.username, password: "", options: { ...editing.options },
        }
      : emptyForm(),
  );
  const [testResult, setTestResult] = useState<TestResult | null>(null);

  const set = <K extends keyof ConnectionInput>(key: K, value: ConnectionInput[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setTestResult(null);
  };

  const payload = (): ConnectionInput => {
    const body: ConnectionInput = { ...form, port: Number(form.port), options: cleanOptions(form) };
    if (!body.password) delete body.password;
    return body;
  };

  const save = useMutation({
    mutationFn: async () => {
      const { kind: _kind, ...changes } = payload();
      const saved = editing ? await api.updateConnection(editing.id, changes) : await api.createConnection(payload());
      // Record a fresh status and capabilities for the saved details; a failure shows in the list.
      await api.testConnection(saved.id).catch(() => undefined);
      return saved;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
      onDone();
    },
  });

  const test = useMutation({
    mutationFn: () => {
      const { name: _name, ...rest } = payload();
      return api.testUnsaved(rest);
    },
    onSuccess: setTestResult,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };

  const isOracle = form.kind === "oracle";

  return (
    <form className="card form" onSubmit={submit}>
      <h2>{editing ? `Edit ${editing.name}` : "New connection"}</h2>
      <div className="grid">
        <label>
          Name
          <input required value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Oracle PROD copy" />
        </label>
        <label>
          Type
          <select
            value={form.kind}
            disabled={!!editing}
            onChange={(e) => {
              const kind = e.target.value as DbKind;
              setForm((f) => ({ ...f, kind, port: DEFAULT_PORT[kind], options: {} }));
              setTestResult(null);
            }}
          >
            <option value="oracle">Oracle</option>
            <option value="postgres">PostgreSQL</option>
          </select>
        </label>
        <label>
          Host
          <input required value={form.host} onChange={(e) => set("host", e.target.value)} placeholder="db.internal" />
        </label>
        <label>
          Port
          <input required type="number" min={1} max={65535} value={form.port} onChange={(e) => set("port", Number(e.target.value))} />
        </label>
        <label>
          {isOracle ? (form.options.use_sid ? "SID" : "Service name") : "Database"}
          <input required value={form.database} onChange={(e) => set("database", e.target.value)} placeholder={isOracle ? "ORCLPDB1" : "appdb"} />
        </label>
        <label>
          Username
          <input required value={form.username} onChange={(e) => set("username", e.target.value)} autoComplete="off" />
        </label>
        <label>
          Password
          <input
            type="password"
            required={!editing}
            value={form.password ?? ""}
            onChange={(e) => set("password", e.target.value)}
            placeholder={editing ? "Leave blank to keep the saved password" : ""}
            autoComplete="new-password"
          />
        </label>
        {isOracle ? (
          <label className="checkbox">
            <input
              type="checkbox"
              checked={!!form.options.use_sid}
              onChange={(e) => set("options", { ...form.options, use_sid: e.target.checked })}
            />
            Connect with a SID instead of a service name
          </label>
        ) : (
          <label>
            SSL mode
            <select value={form.options.sslmode ?? ""} onChange={(e) => set("options", { ...form.options, sslmode: e.target.value || null })}>
              {SSL_MODES.map((m) => (
                <option key={m} value={m}>{m || "default (prefer)"}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      {isOracle && (
        <p className="muted small">
          Use a read-only account: <code>CREATE SESSION</code> plus <code>SELECT ANY DICTIONARY</code> (or{" "}
          <code>SELECT_CATALOG_ROLE</code>) is enough to scan every schema.
        </p>
      )}

      <ErrorBanner error={save.error ?? test.error} />
      {testResult && (
        <div className={`banner ${testResult.ok ? "banner-ok" : "banner-error"}`}>
          {testResult.ok ? `✓ ${testResult.message} — ${testResult.server_version} (${testResult.duration_ms} ms)` : testResult.message}
        </div>
      )}

      <div className="actions">
        <button type="button" className="btn" onClick={() => test.mutate()} disabled={test.isPending || !form.password}
          title={form.password ? "" : "Enter the password to test before saving"}>
          {test.isPending ? "Testing…" : "Test"}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onDone}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
  );
}

export default function ConnectionsPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const connections = useQuery({ queryKey: ["connections"], queryFn: api.connections });
  const [editing, setEditing] = useState<Connection | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["connections"] });

  const run = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setActionError(null);
    try {
      await fn();
    } catch (err) {
      setActionError(err);
    } finally {
      setBusy(null);
      refresh();
    }
  };

  const openForm = (conn: Connection | null) => {
    setEditing(conn);
    setShowForm(true);
  };

  return (
    <>
      <PageHeader
        title="Connections"
        subtitle="Oracle sources and PostgreSQL targets. Passwords are stored encrypted and never returned by the API."
        actions={!showForm && <button className="btn btn-primary" onClick={() => openForm(null)}>New connection</button>}
      />

      {showForm && <ConnectionForm key={editing?.id ?? "new"} editing={editing} onDone={() => setShowForm(false)} />}

      <ErrorBanner error={connections.error ?? actionError} />

      {connections.data?.length === 0 && !showForm && (
        <EmptyState>No connections yet. Add the Oracle database you want to scan to get started.</EmptyState>
      )}

      {!!connections.data?.length && (
        <div className="card table-card">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Endpoint</th>
                <th>Status</th>
                <th>Last tested</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {connections.data.map((conn) => (
                <tr key={conn.id}>
                  <td>
                    <div className="cell-title">{conn.name} <KindTag kind={conn.kind} /></div>
                    <CapabilitySummary conn={conn} />
                  </td>
                  <td className="mono small">
                    {conn.username}@{conn.host}:{conn.port}/{conn.database}
                  </td>
                  <td>
                    <TestBadge ok={conn.last_test_ok} />
                    {conn.last_test_ok === false && <div className="small error-text">{conn.last_test_message}</div>}
                  </td>
                  <td className="small">{formatTime(conn.last_tested_at)}</td>
                  <td className="row-actions">
                    <button className="btn btn-sm" disabled={busy !== null} onClick={() => run(`test-${conn.id}`, () => api.testConnection(conn.id))}>
                      {busy === `test-${conn.id}` ? "Testing…" : "Test"}
                    </button>
                    {conn.kind === "oracle" && (
                      <button
                        className="btn btn-sm"
                        disabled={busy !== null}
                        title="Run ora2pg -t SHOW_VERSION through the worker"
                        onClick={() => run(`o2p-${conn.id}`, async () => navigate(`/jobs/${(await api.ora2pgCheck(conn.id)).id}`))}
                      >
                        Ora2Pg check
                      </button>
                    )}
                    <button className="btn btn-sm btn-ghost" onClick={() => openForm(conn)}>Edit</button>
                    <button
                      className="btn btn-sm btn-ghost danger"
                      disabled={busy !== null}
                      onClick={() => {
                        if (confirm(`Delete connection "${conn.name}"?`)) run(`del-${conn.id}`, () => api.deleteConnection(conn.id));
                      }}
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
