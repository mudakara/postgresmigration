from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://studio:studio@localhost:5432/studio"
    redis_url: str = "redis://localhost:6379/0"

    # Fernet key used to encrypt stored database passwords. Generate one with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    credentials_key: str

    # "thin" needs no Oracle client libraries. Switch to "thick" (uses the bundled
    # Instant Client) if the Oracle server requires Native Network Encryption.
    oracle_client_mode: Literal["thin", "thick"] = "thin"
    oracle_home: Path = Path("/opt/oracle/instantclient")

    ora2pg_bin: str = "ora2pg"
    ora2pg_timeout_seconds: int = 3600
    # Ora2Pg's cost report, per schema. It can be very slow without DBA_* access (ALL_* views).
    ora2pg_report_timeout_seconds: int = 900
    target_pg_version: int = 18

    # Claude review of Oracle/PostgreSQL pairs. Only source code is sent, never table data.
    # The SDK reads ANTHROPIC_API_KEY from the environment.
    # Needed only for API keys that aren't scoped to a workspace (sent as anthropic-workspace-id).
    anthropic_workspace_id: str | None = None
    review_model: str = "claude-opus-5-5"
    review_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    review_concurrency: int = 4
    review_max_source_chars: int = 150_000
    # Schema explorer performance review: objects per run, and how much source per request.
    perf_review_max_objects: int = 300
    perf_review_batch_chars: int = 60_000
    perf_review_batch_objects: int = 8

    # Local throwaway Postgres (compose service scratch-pg) with plpgsql_check. When the
    # target lacks plpgsql_check, the comparison copies the target's schema (structure only,
    # no data) into a temporary database here, checks the code, and drops the database.
    compile_in_scratch: bool = True
    scratch_db_host: str = "scratch-pg"
    scratch_db_port: int = 5432
    scratch_db_user: str = "scratch"
    scratch_db_password: str | None = None
    scratch_db_name: str = "scratch"

    work_dir: Path = Path("/data/work")
    connect_timeout_seconds: int = 10
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
