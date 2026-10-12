import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator, model_validator

from app.models import DbKind, JobStatus, ScanStatus

# These values are written into Ora2Pg's config and DBI DSN, where ';', whitespace and
# line breaks are separators, so restrict them to characters real names use.
Host = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:\[\]-]+$", max_length=255)]
DbName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._$#-]+$", max_length=255)]
UserName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._$#@-]+$", max_length=128)]
SchemaName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_$#]+$", max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Password = Annotated[SecretStr, Field(min_length=1, max_length=1024)]


class ConnectionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Oracle: treat `database` as a SID instead of a service name.
    use_sid: bool = False
    # Oracle: force Ora2Pg's USER_GRANTS (ALL_* views). None = decide from the last test.
    user_grants: bool | None = None
    # Postgres only.
    sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] | None = None


class EndpointFields(BaseModel):
    kind: DbKind
    host: Host
    port: int = Field(ge=1, le=65535)
    database: DbName
    username: UserName
    options: ConnectionOptions = ConnectionOptions()

    @model_validator(mode="after")
    def options_match_kind(self):
        opts = self.options
        if self.kind is DbKind.POSTGRES and (opts.use_sid or opts.user_grants is not None):
            raise ValueError("use_sid and user_grants only apply to Oracle connections")
        if self.kind is DbKind.ORACLE and opts.sslmode is not None:
            raise ValueError("sslmode only applies to Postgres connections")
        return self


class ConnectionTestRequest(EndpointFields):
    password: Password


class ConnectionCreate(EndpointFields):
    name: Name
    password: Password


class ConnectionUpdate(BaseModel):
    """Partial update. The kind is fixed once created; omit password to keep the stored one."""

    name: Name | None = None
    host: Host | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database: DbName | None = None
    username: UserName | None = None
    password: Password | None = None
    options: ConnectionOptions | None = None


class ConnectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: DbKind
    host: str
    port: int
    database: str
    username: str
    options: dict[str, Any]
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_test_message: str | None
    server_version: str | None
    capabilities: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    server_version: str | None
    capabilities: dict[str, Any]
    duration_ms: int


_PG_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _validate_project_settings(settings: dict[str, Any] | None) -> dict[str, Any] | None:
    """target_schemas must be plain schema names: they select what the comparison reads."""
    if settings is None or "target_schemas" not in settings:
        return settings
    names = settings["target_schemas"]
    if not isinstance(names, list) or not all(isinstance(n, str) and _PG_SCHEMA.match(n) for n in names):
        raise ValueError("settings.target_schemas must be a list of plain PostgreSQL schema names")
    return settings


class ProjectCreate(BaseModel):
    name: Name
    description: str | None = Field(default=None, max_length=5000)
    oracle_connection_id: uuid.UUID
    postgres_connection_id: uuid.UUID | None = None
    schemas: list[SchemaName] = Field(default_factory=list, max_length=500)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, value):
        return _validate_project_settings(value)


class ProjectUpdate(BaseModel):
    name: Name | None = None
    description: str | None = Field(default=None, max_length=5000)
    oracle_connection_id: uuid.UUID | None = None
    postgres_connection_id: uuid.UUID | None = None
    schemas: list[SchemaName] | None = Field(default=None, max_length=500)
    settings: dict[str, Any] | None = None

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, value):
        return _validate_project_settings(value)


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    oracle_connection_id: uuid.UUID
    postgres_connection_id: uuid.UUID | None
    schemas: list[str]
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class JobSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    status: JobStatus
    project_id: uuid.UUID | None
    connection_id: uuid.UUID | None
    progress: float | None
    progress_message: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobRead(JobSummary):
    params: dict[str, Any]
    result: dict[str, Any] | None
    log: str | None


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    redis: bool


class SystemInfo(BaseModel):
    ora2pg_version: str | None
    instant_client: str | None
    oracle_client_mode: str
    target_pg_version: int
    job_types: list[str]


# --- scans & inventory -----------------------------------------------------------

class ScanCreate(BaseModel):
    include_ora2pg_cost: bool = True


class ScanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    job_id: uuid.UUID | None
    status: ScanStatus
    include_ora2pg_cost: bool
    schemas: list[str]
    oracle_version: str | None
    dictionary: str | None
    summary: dict[str, Any]
    object_counts: list[dict[str, Any]]
    ora2pg_reports: list[dict[str, Any]]
    warnings: list[str]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class FindingRead(BaseModel):
    code: str
    title: str
    category: str
    severity: str
    count: int
    lines: list[int]
    detail: str | None = None


class SourceObjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner: str
    name: str
    object_type: str
    status: str
    line_count: int
    complexity: str
    effort: float
    ora2pg_cost: float | None
    unsafe_to_execute: bool
    wrapped: bool
    finding_codes: list[str]
    sys_packages: list[str]
    oracle_last_ddl: datetime | None
    source_changed_scan_id: uuid.UUID | None
    removed_at: datetime | None


class ObjectPage(BaseModel):
    total: int
    items: list[SourceObjectSummary]


class SourceRoutineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    package_name: str | None
    name: str
    overload: str
    kind: str
    return_type: str | None
    arguments: list[dict[str, Any]]
    signature: str
    attributes: dict[str, Any]
    ora2pg_cost: float | None


class DependencyRead(BaseModel):
    owner: str
    name: str
    object_type: str | None
    link: str | None = None
    object_id: uuid.UUID | None = None


class SourceObjectDetail(SourceObjectSummary):
    source: str | None
    findings: list[FindingRead]
    metadata: dict[str, Any]
    oracle_created: datetime | None
    first_seen_scan_id: uuid.UUID | None
    last_seen_scan_id: uuid.UUID | None
    routines: list[SourceRoutineRead]
    depends_on: list[DependencyRead]
    used_by: list[DependencyRead]


class RuleRead(BaseModel):
    code: str
    title: str
    category: str
    severity: str
    unsafe: bool
    advice: str


class InventoryReport(BaseModel):
    project_id: uuid.UUID
    last_scan: ScanRead | None
    last_successful_scan: ScanRead | None
    totals: dict[str, Any]
    effort: dict[str, Any]
    by_type: list[dict[str, Any]]
    by_owner: list[dict[str, Any]]
    complexity: list[dict[str, Any]]
    categories: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    sys_packages: list[dict[str, Any]]
    top_effort: list[dict[str, Any]]


# --- comparison (Scenario 2) ------------------------------------------------------

class ComparisonRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    job_id: uuid.UUID | None
    status: ScanStatus
    target_version: str | None
    summary: dict[str, Any]
    warnings: list[str]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TargetObjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schema_name: str = Field(validation_alias="schema")
    name: str
    kind: str
    identity_args: str
    result_type: str | None
    language: str | None
    display_name: str
    removed_at: datetime | None


class TargetObjectDetail(TargetObjectSummary):
    arguments: list[dict[str, Any]]
    n_defaults: int
    source: str | None
    check_results: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(validation_alias="metadata_")


class PairSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    unit_kind: str
    oracle_name: str
    source_object_id: uuid.UUID
    target_object_id: uuid.UUID | None
    target_name: str | None = None
    status: str
    match_method: str | None
    match_confidence: float | None
    manual: bool
    signature_status: str | None
    risk: str
    finding_count: int = 0
    ai_verdict: str | None
    ai_stale: bool
    review_status: str
    updated_at: datetime


class PairPage(BaseModel):
    total: int
    items: list[PairSummary]


class OracleUnitRead(BaseModel):
    kind: str
    display_name: str
    routine_kind: str | None
    signature: str | None
    arguments: list[dict[str, Any]]
    return_type: str | None
    source_object_id: uuid.UUID
    object_label: str
    source: str
    member_range: list[int] | None


class PairDetail(PairSummary):
    alternatives: list[TargetObjectSummary]
    signature_diffs: list[dict[str, Any]]
    static_findings: list[dict[str, Any]]
    ai_review: dict[str, Any] | None
    review_note: str | None
    oracle: OracleUnitRead | None
    target: TargetObjectDetail | None


class PairUpdate(BaseModel):
    """Set exactly one of: a manual target, ignore, or reset to automatic matching —
    and/or the human review status."""

    target_object_id: uuid.UUID | None = None
    ignore: bool | None = None
    reset: bool | None = None
    review_status: Literal["unreviewed", "confirmed_ok", "needs_fix", "fixed", "wont_fix"] | None = None
    review_note: str | None = Field(default=None, max_length=5000)


class ReviewRequest(BaseModel):
    scope: Literal["risky", "unreviewed", "all_matched", "selected"] = "risky"
    pair_ids: list[uuid.UUID] | None = None


class ReviewEstimate(BaseModel):
    pairs: int
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    credentials_configured: bool


class ComparisonOverview(BaseModel):
    last_run: ComparisonRunRead | None
    last_successful_run: ComparisonRunRead | None
    counts: dict[str, Any]
    conventions: dict[str, Any]
    review: dict[str, Any]


# --- schema explorer ----------------------------------------------------------------


class CatalogSnapshotCreate(BaseModel):
    # Empty: every user schema the connection can see.
    schemas: list[SchemaName] = Field(default_factory=list, max_length=500)


class CatalogSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    job_id: uuid.UUID | None
    status: ScanStatus
    requested_schemas: list[str]
    schemas: list[str]
    server_version: str | None
    dictionary: str | None
    summary: dict[str, Any]
    warnings: list[str]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CatalogTableSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schema_name: str = Field(validation_alias="schema")
    name: str
    kind: str
    row_estimate: int | None
    partitions: int
    column_count: int
    references_count: int
    referenced_by_count: int
    code_count: int
    trigger_count: int
    index_count: int
    unindexed_fk_count: int


class CatalogTablePage(BaseModel):
    total: int
    items: list[CatalogTableSummary]


class CatalogCodeSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schema_name: str = Field(validation_alias="schema")
    name: str
    kind: str
    signature: str | None
    language: str | None
    status: str | None
    table_id: uuid.UUID | None
    table_count: int
    extra: dict[str, Any]


class CatalogCodePage(BaseModel):
    total: int
    items: list[CatalogCodeSummary]


class CatalogRelationRead(BaseModel):
    id: uuid.UUID
    kind: str
    name: str | None
    # The table at the other end, and whether it is in this snapshot (has an id).
    other_table_id: uuid.UUID | None
    other_label: str
    other_kind: str | None
    columns: list[str]
    other_columns: list[str]
    details: dict[str, Any]


class CatalogUsageRead(BaseModel):
    operations: list[str]
    found_by: list[str]
    subprograms: list[str]
    lines: list[int]
    member_operations: dict[str, list[str]] = {}


class CatalogCodeUsage(CatalogUsageRead):
    code: CatalogCodeSummary


class CatalogTableUsage(CatalogUsageRead):
    table: CatalogTableSummary


class CatalogTableDetail(CatalogTableSummary):
    snapshot_id: uuid.UUID
    columns: list[dict[str, Any]]
    primary_key: dict[str, Any] | None
    unique_keys: list[dict[str, Any]]
    indexes: list[dict[str, Any]]
    comment: str | None
    references: list[CatalogRelationRead] = []  # foreign keys from this table
    referenced_by: list[CatalogRelationRead] = []  # foreign keys pointing at it
    reads_from: list[CatalogRelationRead] = []  # views: the tables they read
    read_by_views: list[CatalogRelationRead] = []
    used_by: list[CatalogCodeUsage] = []
    triggers: list[CatalogCodeSummary] = []


class CatalogCodeDetail(CatalogCodeSummary):
    snapshot_id: uuid.UUID
    source: str | None
    uses: list[CatalogTableUsage] = []
    table: CatalogTableSummary | None = None  # triggers: the table they fire on


class PerfReviewRequest(BaseModel):
    # triggers: triggers on the table; writers: those plus code that writes to it;
    # all: every object that uses it; selected: code_ids.
    scope: Literal["triggers", "writers", "all", "selected"] = "writers"
    code_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)


class PerfReviewEstimate(BaseModel):
    objects: int
    batches: int
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    credentials_configured: bool
    max_objects: int
    code_ids: list[str]


class PerfReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID | None
    status: ScanStatus
    scope: str
    model: str | None
    table_schema: str
    table_name: str
    results: dict[str, Any]
    summary: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class PerfReviewState(BaseModel):
    latest: PerfReviewRead | None
    active: PerfReviewRead | None
    credentials_configured: bool
    model: str


# --- sequence check -------------------------------------------------------------------


class SequenceCheckCreate(BaseModel):
    postgres_connection_id: uuid.UUID
    # Also compare with Oracle's sequence values (LAST_NUMBER), matched by schema and name.
    oracle_connection_id: uuid.UUID | None = None
    schemas: list[SchemaName] = Field(default_factory=list, max_length=500)


class SequenceCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    postgres_connection_id: uuid.UUID
    oracle_connection_id: uuid.UUID | None
    job_id: uuid.UUID | None
    status: ScanStatus
    requested_schemas: list[str]
    schemas: list[str]
    summary: dict[str, Any]
    warnings: list[str]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SequenceCheckDetail(SequenceCheckRead):
    sequences: list[dict[str, Any]]
