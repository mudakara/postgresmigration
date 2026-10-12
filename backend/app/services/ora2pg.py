"""Thin wrapper around the ora2pg CLI.

Each run gets a private workspace directory holding a generated config file and the
output directory. The Oracle password is passed through the ORA2PG_PASSWD environment
variable, so it never appears in the config file or on the command line.
"""

import codecs
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.models import DbKind
from app.services.endpoints import Endpoint

ConfigValue = str | int | Path
PROGRESS_INTERVAL_SECONDS = 5


class Ora2PgError(Exception):
    pass


@dataclass
class Ora2PgResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    output_dir: Path

    @property
    def succeeded(self) -> bool:
        # Some fatal errors are printed with a zero exit status.
        return self.returncode == 0 and "FATAL:" not in self.stdout

    def combined_output(self, limit: int = 200_000) -> str:
        text = self.stdout + (f"\n--- stderr ---\n{self.stderr}" if self.stderr.strip() else "")
        return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]

    def failure_summary(self) -> str:
        lines = [ln for ln in (self.stdout + "\n" + self.stderr).splitlines() if ln.strip()]
        fatal = [ln for ln in lines if "FATAL" in ln or "ORA-" in ln or "DBI connect" in ln]
        detail = "\n".join((fatal or lines)[-5:])
        return f"ora2pg exited with status {self.returncode}: {detail}"[:2000]


def _directive(name: str, value: ConfigValue) -> str:
    text = str(value)
    # Ora2Pg reads one directive per line and trims surrounding whitespace, so a line
    # break would let a value inject extra directives.
    if "\n" in text or "\r" in text or text != text.strip() or not text:
        raise ValueError(f"Invalid value for Ora2Pg directive {name}")
    return f"{name}\t{text}"


def build_dsn(endpoint: Endpoint) -> str:
    key = "sid" if endpoint.options.get("use_sid") else "service_name"
    return f"dbi:Oracle:host={endpoint.host};{key}={endpoint.database};port={endpoint.port}"


def render_config(
    endpoint: Endpoint, *, user_grants: bool, extra: Mapping[str, ConfigValue] | None = None
) -> str:
    if endpoint.kind is not DbKind.ORACLE:
        raise ValueError("Ora2Pg needs an Oracle connection")
    settings = get_settings()
    directives: dict[str, ConfigValue] = {
        "ORACLE_HOME": settings.oracle_home,
        "ORACLE_DSN": build_dsn(endpoint),
        "ORACLE_USER": endpoint.username,
        "USER_GRANTS": int(user_grants),
        # We only ever read from Oracle.
        "TRANSACTION": "readonly",
        "NLS_LANG": "AMERICAN_AMERICA.AL32UTF8",
        "PG_VERSION": settings.target_pg_version,
        "DEBUG": 0,
    }
    directives.update(extra or {})
    return "\n".join(_directive(k, v) for k, v in directives.items()) + "\n"


@contextmanager
def workspace() -> Iterator[Path]:
    base = get_settings().work_dir
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="ora2pg-", dir=base))  # created 0700
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _child_env(password: str | None = None) -> dict[str, str]:
    settings = get_settings()
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "ORACLE_HOME": str(settings.oracle_home),
        "LD_LIBRARY_PATH": str(settings.oracle_home),
        "NLS_LANG": "AMERICAN_AMERICA.AL32UTF8",
        "LANG": "C.UTF-8",
    }
    if password is not None:
        env["ORA2PG_PASSWD"] = password
    return env


def _read_stream(stream, sink: list[str], latest: list[str]) -> None:
    """Collect a pipe's output; remember the last non-empty progress line.

    read1 returns as soon as any bytes arrive, so progress shows up while ora2pg runs
    (its progress bars use carriage returns, not newlines)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    for raw in iter(lambda: stream.read1(4096), b""):
        chunk = decoder.decode(raw)
        sink.append(chunk)
        lines = [ln.strip() for ln in chunk.replace("\r", "\n").split("\n") if ln.strip()]
        if lines:
            latest[0] = lines[-1]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run(
    endpoint: Endpoint,
    export_type: str,
    work: Path,
    *,
    args: Sequence[str] = (),
    config: Mapping[str, ConfigValue] | None = None,
    user_grants: bool = False,
    timeout: int | None = None,
    on_progress: Callable[[str, float], None] | None = None,
) -> Ora2PgResult:
    """Run ora2pg and wait for it.

    ora2pg runs in its own process group so a timeout kills it together with any
    children (which could otherwise keep the pipes open and hang the wait). While it
    runs, `on_progress(last_stderr_line, elapsed_seconds)` is called from this thread
    every few seconds.
    """
    settings = get_settings()
    limit = timeout or settings.ora2pg_timeout_seconds
    conf_path = work / "ora2pg.conf"
    output_dir = work / "output"
    output_dir.mkdir(exist_ok=True)

    fd = os.open(conf_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(render_config(endpoint, user_grants=user_grants, extra=config))

    cmd = [
        settings.ora2pg_bin, "-c", str(conf_path), "-t", export_type, "-b", str(output_dir),
        *args,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=work, env=_child_env(endpoint.password), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except FileNotFoundError as exc:
        conf_path.unlink(missing_ok=True)
        raise Ora2PgError(f"ora2pg executable not found: {settings.ora2pg_bin}") from exc

    out: list[str] = []
    err: list[str] = []
    latest = [""]
    readers = [
        threading.Thread(target=_read_stream, args=(proc.stdout, out, [""]), daemon=True),
        threading.Thread(target=_read_stream, args=(proc.stderr, err, latest), daemon=True),
    ]
    for t in readers:
        t.start()
    try:
        while True:
            try:
                proc.wait(timeout=PROGRESS_INTERVAL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - started
                if elapsed > limit:
                    _kill_group(proc)
                    proc.wait()
                    where = f" while: {latest[0]}" if latest[0] else ""
                    raise Ora2PgError(f"ora2pg {export_type} timed out after {int(limit)}s{where}")
                if on_progress is not None:
                    on_progress(latest[0], elapsed)
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        conf_path.unlink(missing_ok=True)
        for t in readers:
            t.join(timeout=5)

    return Ora2PgResult(
        args=cmd,
        returncode=proc.returncode,
        stdout="".join(out),
        stderr="".join(err),
        duration_seconds=round(time.monotonic() - started, 3),
        output_dir=output_dir,
    )


@lru_cache
def ora2pg_version() -> str | None:
    try:
        proc = subprocess.run(
            [get_settings().ora2pg_bin, "--version"],
            capture_output=True, text=True, timeout=30, env=_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None
