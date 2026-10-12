export type DbKind = "oracle" | "postgres";
export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface ConnectionOptions {
  use_sid?: boolean;
  user_grants?: boolean | null;
  sslmode?: string | null;
}

export interface Connection {
  id: string;
  name: string;
  kind: DbKind;
  host: string;
  port: number;
  database: string;
  username: string;
  options: ConnectionOptions;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
  last_test_message: string | null;
  server_version: string | null;
  capabilities: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ConnectionInput {
  name: string;
  kind: DbKind;
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  options: ConnectionOptions;
}

export interface TestResult {
  ok: boolean;
  message: string;
  server_version: string | null;
  capabilities: Record<string, unknown>;
  duration_ms: number;
}

export interface Project {
  id: string;
  name: string;
  description: string | null;
  oracle_connection_id: string;
  postgres_connection_id: string | null;
  schemas: string[];
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type ProjectInput = Pick<
  Project,
  "name" | "description" | "oracle_connection_id" | "postgres_connection_id" | "schemas"
> & { settings?: Record<string, unknown> };

export interface JobSummary {
  id: string;
  type: string;
  status: JobStatus;
  project_id: string | null;
  connection_id: string | null;
  progress: number | null;
  progress_message: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Job extends JobSummary {
  params: Record<string, unknown>;
  result: Record<string, unknown> | null;
  log: string | null;
}

export interface Health {
  status: "ok" | "degraded";
  database: boolean;
  redis: boolean;
}

export interface SystemInfo {
  ora2pg_version: string | null;
  instant_client: string | null;
  oracle_client_mode: string;
  target_pg_version: number;
  job_types: string[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function describeDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const loc = Array.isArray(d?.loc) ? d.loc.filter((p: unknown) => p !== "body").join(".") : "";
        return loc ? `${loc}: ${d.msg}` : String(d?.msg ?? d);
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    let message = `${resp.status} ${resp.statusText}`;
    try {
      const data = await resp.json();
      if (data?.detail !== undefined) message = describeDetail(data.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, message);
  }
  return resp.status === 204 ? (undefined as T) : resp.json();
}

export const api = {
  health: () => request<Health>("GET", "/health"),
  system: () => request<SystemInfo>("GET", "/system"),

  connections: () => request<Connection[]>("GET", "/connections"),
  createConnection: (input: ConnectionInput) => request<Connection>("POST", "/connections", input),
  updateConnection: (id: string, input: Partial<ConnectionInput>) =>
    request<Connection>("PATCH", `/connections/${id}`, input),
  deleteConnection: (id: string) => request<void>("DELETE", `/connections/${id}`),
  testConnection: (id: string) => request<TestResult>("POST", `/connections/${id}/test`),
  testUnsaved: (input: Omit<ConnectionInput, "name">) =>
    request<TestResult>("POST", "/connections/test", input),
  ora2pgCheck: (id: string) => request<Job>("POST", `/connections/${id}/ora2pg-check`),

  projects: () => request<Project[]>("GET", "/projects"),
  createProject: (input: ProjectInput) => request<Project>("POST", "/projects", input),
  updateProject: (id: string, input: Partial<ProjectInput>) =>
    request<Project>("PATCH", `/projects/${id}`, input),
  deleteProject: (id: string) => request<void>("DELETE", `/projects/${id}`),

  jobs: (params: Record<string, string> = {}) =>
    request<JobSummary[]>("GET", `/jobs?${new URLSearchParams(params)}`),
  job: (id: string) => request<Job>("GET", `/jobs/${id}`),
};

// --- scans & inventory ------------------------------------------------------------

export type Category = "blocker" | "execution_safety" | "semantic_risk" | "conversion";
export type Severity = "high" | "medium" | "low";
export type Tier = "simple" | "moderate" | "complex" | "very_complex";

export interface Scan {
  id: string;
  project_id: string;
  job_id: string | null;
  status: JobStatus;
  include_ora2pg_cost: boolean;
  schemas: string[];
  oracle_version: string | null;
  dictionary: string | null;
  summary: Record<string, any>;
  object_counts: { owner: string; object_type: string; count: number; invalid: number }[];
  ora2pg_reports: {
    schema: string; ok: boolean; error: string | null; total_cost: number | null;
    human_days: number | null; migration_level: string | null; duration_seconds: number | null;
  }[];
  warnings: string[];
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ObjectSummary {
  id: string;
  owner: string;
  name: string;
  object_type: string;
  status: string;
  line_count: number;
  complexity: Tier;
  effort: number;
  ora2pg_cost: number | null;
  unsafe_to_execute: boolean;
  wrapped: boolean;
  finding_codes: string[];
  sys_packages: string[];
  oracle_last_ddl: string | null;
  source_changed_scan_id: string | null;
  removed_at: string | null;
}

export interface Finding {
  code: string;
  title: string;
  category: Category;
  severity: Severity;
  count: number;
  lines: number[];
  detail: string | null;
}

export interface Routine {
  id: string;
  package_name: string | null;
  name: string;
  overload: string;
  kind: string;
  return_type: string | null;
  arguments: { position: number; name: string | null; mode: string; type: string; defaulted: boolean }[];
  signature: string;
  attributes: Record<string, unknown>;
  ora2pg_cost: number | null;
}

export interface Dependency {
  owner: string;
  name: string;
  object_type: string | null;
  link: string | null;
  object_id: string | null;
}

export interface ObjectDetail extends ObjectSummary {
  source: string | null;
  findings: Finding[];
  metadata: Record<string, any>;
  oracle_created: string | null;
  first_seen_scan_id: string | null;
  last_seen_scan_id: string | null;
  routines: Routine[];
  depends_on: Dependency[];
  used_by: Dependency[];
}

export interface Rule {
  code: string;
  title: string;
  category: Category;
  severity: Severity;
  unsafe: boolean;
  advice: string;
}

export interface ReportFinding extends Omit<Rule, "unsafe"> {
  unsafe: boolean;
  objects: number;
  occurrences: number;
}

export interface Report {
  project_id: string;
  last_scan: Scan | null;
  last_successful_scan: Scan | null;
  totals: {
    objects: number; lines: number; invalid: number; wrapped: number; unsafe_to_execute: number;
    routines: { standalone: number; package_members: number; procedures: number; functions: number };
  };
  effort: {
    total_units: number; unit_minutes: number; estimated_hours: number; ora2pg_human_days: number | null;
    objects_costed_by_ora2pg: number; objects_costed_by_heuristic: number;
  };
  by_type: { object_type: string; count: number; invalid: number; lines: number; effort: number }[];
  by_owner: { owner: string; count: number; lines: number; effort: number }[];
  complexity: { tier: Tier; count: number; effort: number }[];
  categories: { category: Category; objects: number }[];
  findings: ReportFinding[];
  sys_packages: { name: string; objects: number }[];
  top_effort: { id: string; owner: string; name: string; object_type: string; effort: number; complexity: Tier; unsafe_to_execute: boolean }[];
}

export interface ObjectPage {
  total: number;
  items: ObjectSummary[];
}

export const inventoryApi = {
  project: (id: string) => request<Project>("GET", `/projects/${id}`),
  startScan: (projectId: string, includeOra2pgCost: boolean) =>
    request<Scan>("POST", `/projects/${projectId}/scans`, { include_ora2pg_cost: includeOra2pgCost }),
  scans: (projectId: string) => request<Scan[]>("GET", `/projects/${projectId}/scans`),
  report: (projectId: string) => request<Report>("GET", `/projects/${projectId}/report`),
  objects: (projectId: string, params: URLSearchParams) =>
    request<ObjectPage>("GET", `/projects/${projectId}/objects?${params}`),
  csvUrl: (projectId: string, params: URLSearchParams) => `/api/projects/${projectId}/objects.csv?${params}`,
  object: (id: string) => request<ObjectDetail>("GET", `/objects/${id}`),
  rules: () => request<Rule[]>("GET", "/rules"),
};

// --- comparison (Scenario 2) ------------------------------------------------------

export type Risk = "high" | "medium" | "low" | "none";

export interface ComparisonRun {
  id: string;
  project_id: string;
  job_id: string | null;
  status: JobStatus;
  target_version: string | null;
  summary: Record<string, any>;
  warnings: string[];
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ComparisonOverview {
  last_run: ComparisonRun | null;
  last_successful_run: ComparisonRun | null;
  counts: {
    units: number;
    status: Record<string, number>;
    kind: Record<string, number>;
    risk: Record<string, number>;
    signature: Record<string, number>;
    ai_verdict: Record<string, number>;
    review_status: Record<string, number>;
    unpaired_targets: number;
    target_objects: number;
  };
  conventions: Record<string, { count: number; label: string }>;
  review: { credentials_configured: boolean; model: string; effort: string; reviewed: number; stale: number };
}

export interface TargetObjectSummary {
  id: string;
  schema_name: string;
  name: string;
  kind: string;
  identity_args: string;
  result_type: string | null;
  language: string | null;
  display_name: string;
  removed_at: string | null;
}

export interface TargetObjectDetail extends TargetObjectSummary {
  arguments: { position: number; name: string | null; mode: string; type: string }[];
  n_defaults: number;
  source: string | null;
  check_results: Record<string, any>[];
  metadata: Record<string, any>;
}

export interface PairSummary {
  id: string;
  unit_kind: string;
  oracle_name: string;
  source_object_id: string;
  target_object_id: string | null;
  target_name: string | null;
  status: "matched" | "missing_in_target" | "ignored";
  match_method: string | null;
  match_confidence: number | null;
  manual: boolean;
  signature_status: "match" | "compatible" | "mismatch" | null;
  risk: Risk;
  finding_count: number;
  ai_verdict: "equivalent" | "differences_found" | "needs_human_review" | null;
  ai_stale: boolean;
  review_status: string;
  updated_at: string;
}

export interface SignatureDiff { code: string; severity: Severity; message: string; position: number | null }
export interface StaticFinding {
  code: string; severity: Severity; title: string; detail: string; oracle_lines: number[]; postgres_lines: number[];
}
export interface AiIssue {
  severity: Severity; category: string; title: string; explanation: string;
  oracle_lines: number[]; postgres_lines: number[]; suggested_fix: string;
}
export interface AiReview {
  verdict: "equivalent" | "differences_found" | "needs_human_review";
  confidence: "high" | "medium" | "low";
  summary: string;
  issues: AiIssue[];
  model: string;
  reviewed_at: string;
  usage: Record<string, number>;
}

export interface PairDetail extends PairSummary {
  alternatives: TargetObjectSummary[];
  signature_diffs: SignatureDiff[];
  static_findings: StaticFinding[];
  ai_review: AiReview | null;
  review_note: string | null;
  oracle: {
    kind: string; display_name: string; routine_kind: string | null; signature: string | null;
    arguments: { position: number; name: string | null; mode: string; type: string; defaulted: boolean }[];
    return_type: string | null; source_object_id: string; object_label: string; source: string;
    member_range: [number, number] | null;
  } | null;
  target: TargetObjectDetail | null;
}

export interface PairUpdate {
  target_object_id?: string;
  ignore?: boolean;
  reset?: boolean;
  review_status?: string;
  review_note?: string | null;
}

export type ReviewScope = "risky" | "unreviewed" | "all_matched" | "selected";

export interface ReviewEstimate {
  pairs: number; model: string; input_tokens: number; output_tokens: number;
  estimated_cost_usd: number | null; credentials_configured: boolean;
}

export const compareApi = {
  start: (projectId: string) => request<ComparisonRun>("POST", `/projects/${projectId}/comparisons`),
  runs: (projectId: string) => request<ComparisonRun[]>("GET", `/projects/${projectId}/comparisons`),
  overview: (projectId: string) => request<ComparisonOverview>("GET", `/projects/${projectId}/comparison`),
  pairs: (projectId: string, params: URLSearchParams) => request<{ total: number; items: PairSummary[] }>("GET", `/projects/${projectId}/pairs?${params}`),
  pair: (id: string) => request<PairDetail>("GET", `/pairs/${id}`),
  updatePair: (id: string, body: PairUpdate) => request<PairDetail>("PATCH", `/pairs/${id}`, body),
  targets: (projectId: string, params: Record<string, string>) =>
    request<TargetObjectSummary[]>("GET", `/projects/${projectId}/target-objects?${new URLSearchParams(params)}`),
  estimate: (projectId: string, scope: ReviewScope, pairIds?: string[]) =>
    request<ReviewEstimate>("POST", `/projects/${projectId}/reviews/estimate`, { scope, pair_ids: pairIds }),
  startReviews: (projectId: string, scope: ReviewScope, pairIds?: string[]) =>
    request<Job>("POST", `/projects/${projectId}/reviews`, { scope, pair_ids: pairIds }),
};

// --- schema explorer ----------------------------------------------------------------

export interface CatalogSnapshot {
  id: string;
  connection_id: string;
  job_id: string | null;
  status: JobStatus;
  requested_schemas: string[];
  schemas: string[];
  server_version: string | null;
  dictionary: string | null;
  summary: {
    tables?: number;
    views?: number;
    foreign_keys?: number;
    indexes?: number;
    unindexed_foreign_keys?: number;
    code_table_links?: number;
    code?: Record<string, number>;
    schemas?: { schema: string; tables?: number; views?: number; code?: number }[];
  };
  warnings: string[];
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface CatalogTableSummary {
  id: string;
  schema_name: string;
  name: string;
  kind: string;
  row_estimate: number | null;
  partitions: number;
  column_count: number;
  references_count: number;
  referenced_by_count: number;
  code_count: number;
  trigger_count: number;
  index_count: number;
  unindexed_fk_count: number;
}

export interface CatalogIndex {
  name: string;
  columns: string[];
  include: string[];
  unique: boolean;
  primary: boolean;
  method: string;
  predicate?: string | null;
  status?: string | null;
  partitioned?: boolean;
  definition?: string;
}

export interface CatalogCodeSummary {
  id: string;
  schema_name: string;
  name: string;
  kind: string;
  signature: string | null;
  language: string | null;
  status: string | null;
  table_id: string | null;
  table_count: number;
  extra: Record<string, any>;
}

export interface CatalogRelation {
  id: string;
  kind: "foreign_key" | "view";
  name: string | null;
  other_table_id: string | null;
  other_label: string;
  other_kind: string | null;
  columns: string[];
  other_columns: string[];
  details: Record<string, any>;
}

export interface CatalogUsageInfo {
  operations: string[];
  found_by: ("dictionary" | "code" | "dynamic_sql")[];
  subprograms: string[];
  lines: number[];
  member_operations?: Record<string, string[]>;
}

export interface CatalogColumn {
  name: string;
  type: string;
  nullable: boolean;
  default?: string | null;
  comment?: string | null;
}

export interface CatalogTableDetail extends CatalogTableSummary {
  snapshot_id: string;
  columns: CatalogColumn[];
  primary_key: { name: string; columns: string[] } | null;
  unique_keys: { name: string; columns: string[] }[];
  indexes: CatalogIndex[];
  comment: string | null;
  references: CatalogRelation[];
  referenced_by: CatalogRelation[];
  reads_from: CatalogRelation[];
  read_by_views: CatalogRelation[];
  used_by: (CatalogUsageInfo & { code: CatalogCodeSummary })[];
  triggers: CatalogCodeSummary[];
}

export interface CatalogCodeDetail extends CatalogCodeSummary {
  snapshot_id: string;
  source: string | null;
  table: CatalogTableSummary | null;
  uses: (CatalogUsageInfo & { table: CatalogTableSummary })[];
}

export type PerfScope = "triggers" | "writers" | "all" | "selected";
export type PerfPriority = "high" | "medium" | "low" | "none";

export interface PerfIssue {
  severity: Severity;
  category: string;
  title: string;
  explanation: string;
  lines: number[];
  suggestion: string;
  example: string;
}

export interface PerfObjectResult {
  code_key: string[];
  code_id: string;
  current_code_id: string | null;
  stale: boolean;
  label: string;
  name: string;
  kind: string;
  priority: PerfPriority;
  summary: string;
  issues: PerfIssue[];
}

export interface PerfRecommendation {
  kind: string;
  title: string;
  explanation: string;
  sql: string;
  helps: string[];
}

export interface PerfReview {
  id: string;
  job_id: string | null;
  status: JobStatus;
  scope: PerfScope;
  model: string | null;
  table_schema: string;
  table_name: string;
  results: {
    objects?: PerfObjectResult[];
    table_recommendations?: PerfRecommendation[];
    failed?: { label: string; error: string }[];
  };
  summary: { reviewed?: number; failed?: number; issues?: number; priority?: Record<PerfPriority, number>; usage?: Record<string, number> };
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface PerfReviewState {
  latest: PerfReview | null;
  active: PerfReview | null;
  credentials_configured: boolean;
  model: string;
}

export interface PerfEstimate {
  objects: number;
  batches: number;
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number | null;
  credentials_configured: boolean;
  max_objects: number;
  code_ids: string[];
}

export type DiffStatus = "same" | "equivalent" | "info" | "warning" | "error";

export interface DiffRow<T = any> {
  status: DiffStatus;
  left: T | null;
  right: T | null;
  message: string;
  label?: string;
}

export interface CompareSide {
  connection_id: string;
  connection_name: string;
  kind: DbKind;
  snapshot_id: string;
  read_at: string | null;
  table: CatalogTableDetail;
}

export type CompareSection =
  "table" | "columns" | "keys" | "indexes" | "foreign_keys" | "referenced_by" | "triggers" | "code" | "views";

export interface TableComparison {
  left: CompareSide;
  right: CompareSide;
  names: [string, string];
  sections: Record<CompareSection, DiffRow[]>;
  counts: Record<DiffStatus, number>;
  section_counts: Record<CompareSection, Record<DiffStatus, number>>;
}

export interface OverviewTable {
  left: { id: string; key: string; kind: string; row_estimate: number | null } | null;
  right: { id: string; key: string; kind: string; row_estimate: number | null } | null;
  status: DiffStatus;
  missing: string | null;
  counts: { error: number; warning: number; info: number };
  sections: Partial<Record<CompareSection, { error?: number; warning?: number }>>;
  headline: string;
}

export interface CompareOverview {
  names: [string, string];
  summary: {
    pairs: number; matched: number; with_problems: number; with_differences: number; clean: number;
    only_left: number; only_right: number;
  };
  tables: OverviewTable[];
}

export const catalogApi = {
  snapshots: (connectionId: string) =>
    request<CatalogSnapshot[]>("GET", `/connections/${connectionId}/catalog-snapshots`),
  start: (connectionId: string, schemas: string[]) =>
    request<CatalogSnapshot>("POST", `/connections/${connectionId}/catalog-snapshots`, { schemas }),
  tables: (snapshotId: string, params: URLSearchParams) =>
    request<{ total: number; items: CatalogTableSummary[] }>("GET", `/catalog-snapshots/${snapshotId}/tables?${params}`),
  codeList: (snapshotId: string, params: URLSearchParams) =>
    request<{ total: number; items: CatalogCodeSummary[] }>("GET", `/catalog-snapshots/${snapshotId}/code?${params}`),
  table: (id: string) => request<CatalogTableDetail>("GET", `/catalog-tables/${id}`),
  code: (id: string) => request<CatalogCodeDetail>("GET", `/catalog-code/${id}`),
  findTable: (snapshotId: string, schema: string, name: string) =>
    request<{ matched_by: "exact" | "name"; table: CatalogTableSummary }>(
      "GET", `/catalog-snapshots/${snapshotId}/find-table?${new URLSearchParams({ schema, name })}`),
  compareOverview: (leftSnapshot: string, rightSnapshot: string) =>
    request<CompareOverview>("GET", `/catalog-compare/overview?${new URLSearchParams({ left_snapshot: leftSnapshot, right_snapshot: rightSnapshot })}`),
  compare: (left: string, right: string) =>
    request<TableComparison>("GET", `/catalog-compare?${new URLSearchParams({ left, right })}`),
  perfState: (tableId: string) => request<PerfReviewState>("GET", `/catalog-tables/${tableId}/perf-reviews`),
  perfEstimate: (tableId: string, scope: PerfScope, codeIds?: string[]) =>
    request<PerfEstimate>("POST", `/catalog-tables/${tableId}/perf-reviews/estimate`, { scope, code_ids: codeIds }),
  startPerf: (tableId: string, scope: PerfScope, codeIds?: string[]) =>
    request<PerfReview>("POST", `/catalog-tables/${tableId}/perf-reviews`, { scope, code_ids: codeIds }),
};

// --- sequence check ---------------------------------------------------------------------

export type SequenceStatus =
  | "behind" | "overflow" | "behind_oracle" | "check_failed" | "no_privilege" | "descending" | "unlinked" | "ok";

export interface SequenceColumn {
  schema: string;
  table: string;
  column: string;
  via: "identity" | "serial" | "default" | "trigger" | "insert";
  source: string | null;
  data_type: string | null;
  max_value: number | null;
  error: string | null;
}

export interface SequenceResult {
  schema: string;
  name: string;
  data_type: string;
  increment: number;
  max_value: number;
  last_value: number | null;
  is_called: boolean | null;
  columns: SequenceColumn[];
  oracle: { owner: string; name: string; last_number: number } | null;
  next_value: number | null;
  needed_next: number | null;
  status: SequenceStatus;
  message: string;
}

export interface SequenceCheck {
  id: string;
  postgres_connection_id: string;
  oracle_connection_id: string | null;
  job_id: string | null;
  status: JobStatus;
  requested_schemas: string[];
  schemas: string[];
  summary: {
    sequences?: number;
    linked?: number;
    oracle_matched?: number;
    oracle_compared?: boolean;
    status?: Record<SequenceStatus, number>;
  };
  warnings: string[];
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface SequenceCheckDetail extends SequenceCheck {
  sequences: SequenceResult[];
}

export const sequenceApi = {
  checks: (postgresConnectionId: string) =>
    request<SequenceCheck[]>("GET", `/sequence-checks?postgres_connection_id=${postgresConnectionId}`),
  check: (id: string) => request<SequenceCheckDetail>("GET", `/sequence-checks/${id}`),
  start: (body: { postgres_connection_id: string; oracle_connection_id: string | null; schemas: string[] }) =>
    request<SequenceCheck>("POST", "/sequence-checks", body),
  scriptUrl: (id: string, scope: "needed" | "all") => `/api/sequence-checks/${id}/fix.sql?scope=${scope}`,
  script: async (id: string, scope: "needed" | "all") => {
    const resp = await fetch(`/api/sequence-checks/${id}/fix.sql?scope=${scope}`);
    if (!resp.ok) throw new ApiError(resp.status, `${resp.status} ${resp.statusText}`);
    return resp.text();
  },
};
