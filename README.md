# Migration Studio

**Migration Studio** is a web application that helps a team migrate an Oracle database's code (stored procedures, functions, packages, triggers and views) to PostgreSQL, and check that a migration that has already been done is correct.

It runs locally with Docker and sits on top of [Ora2Pg](https://ora2pg.darold.net/), the standard open-source Oracle-to-PostgreSQL migration tool. It adds what Ora2Pg doesn't have: an inventory you can browse, risk analysis of every object, a side-by-side comparison of Oracle with the migrated PostgreSQL, AI review with Claude, and tools for the problems that appear after a data load. It is built for Oracle 19c → PostgreSQL 18 but works with other recent versions.

The app only **reads** the databases you connect to (sessions are read-only; see [Security](#security-notes)). Anything that changes a database, such as fixing sequences, is produced as a SQL script for you to review and run.

## What it can do

| Page | Use it to… |
|---|---|
| **Overview** | See every connected database's statistics side by side: tables, views, columns, indexes, foreign keys, procedures, functions, packages and triggers, plus tables without a primary key, foreign keys without an index, invalid code and disabled triggers. Click a number to list those items, filter by schema, and open any of them in the Schema explorer. The **LOB data check** counts every table with BLOB/CLOB columns on both sides to find LOB data that didn't migrate. |
| **Connections** | Save Oracle and PostgreSQL connections (passwords are encrypted), test them, and check what the account can see. |
| **Projects → Scan & report** | Inventory every PL/SQL object in Oracle, flag risky constructs, and estimate the migration effort (Ora2Pg cost + the app's own rules). |
| **Projects → Comparison** | For an existing migration: pair every Oracle routine, view and trigger with its PostgreSQL counterpart, compare signatures, run static pitfall checks and `plpgsql_check`, and optionally have Claude review each pair. |
| **Schema explorer** | Browse any connection's tables, columns, indexes, foreign keys (with a relationship diagram), triggers, and the procedures and functions that use each table. Search code, find unindexed foreign keys, and ask Claude which code needs performance work. |
| **Table compare** | Put Oracle and PostgreSQL side by side, compare one table (data types, keys, indexes, foreign keys, triggers) and each stored procedure that uses it (parameters, tables used, code pitfalls), or list every table with problems. |
| **Sequence check** | After loading data, find PostgreSQL sequences that are behind the data (the cause of *duplicate key* errors on insert) and generate a script that fixes them. |
| **Jobs** | Follow long-running work (scans, comparisons, catalog reads, reviews) and read its logs. |

## Requirements

- **Docker** Desktop (macOS/Windows) or Docker Engine with Compose v2 (Linux). Allow it at least 4 GB of memory (8 GB if you also run the Oracle sandbox).
- **Network access** from your machine to the Oracle and PostgreSQL servers you want to use (VPN if needed).
- **A read-only Oracle account**, ideally with `SELECT ANY DICTIONARY` (see [Connecting your Oracle 19c](#connecting-your-oracle-19c)), and a PostgreSQL login that can read the migrated schemas (`pg_read_all_data` is enough).
- *Optional:* an [Anthropic API key](https://console.anthropic.com/) for the Claude reviews. Everything else works without it.
- `make` and `openssl` are convenient but not required (the equivalent commands are shown below).

Everything else (Python, Node, Ora2Pg, Perl, DBD::Oracle, the Oracle Instant Client, PostgreSQL) is installed inside the containers. The images build for both amd64 and arm64 (Apple Silicon).

## Installation

1. **Get the code.**

   ```bash
   git clone https://github.com/mudakara/postgresmigration.git
   cd postgresmigration
   ```

2. **Create the configuration file `.env`** with freshly generated secrets.

   ```bash
   make init
   ```

   Without `make`, run the same thing by hand:

   ```bash
   cp .env.example .env && chmod 600 .env
   sed -i.bak \
     -e "s|^CREDENTIALS_KEY=.*|CREDENTIALS_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" \
     -e "s|^APP_DB_PASSWORD=.*|APP_DB_PASSWORD=$(openssl rand -hex 16)|" \
     -e "s|^SCRATCH_DB_PASSWORD=.*|SCRATCH_DB_PASSWORD=$(openssl rand -hex 16)|" \
     -e "s|^ORACLE_SANDBOX_SYS_PASSWORD=.*|ORACLE_SANDBOX_SYS_PASSWORD=Sys_$(openssl rand -hex 8)|" \
     .env && rm .env.bak
   ```

   **Back up `.env`.** `CREDENTIALS_KEY` encrypts the saved database passwords; if it is lost or changed, they can't be decrypted and must be re-entered. `.env` is git-ignored: never commit it.

3. *Optional:* **enable the Claude features** by adding your key to `.env`:

   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   # only if your key belongs to a specific workspace:
   ANTHROPIC_WORKSPACE_ID=wrkspc_...
   ```

4. **Build and start** (the first build downloads the Oracle Instant Client and builds Ora2Pg, so it takes several minutes):

   ```bash
   make up
   ```

   or `docker compose up -d --build`. Check that everything is running with `docker compose ps`: `api`, `worker`, `ui`, `app-db`, `redis` and `scratch-pg` should be up.

5. **Open http://localhost:5173.** The sidebar shows "Backend healthy" when the API, database and job queue are reachable.

**Updating:** `git pull`, then `make up` again. Database migrations run automatically when the `api` container starts.
**Stopping:** `make down` (or `docker compose down`). Your data (connections, scans, snapshots) is kept in Docker volumes.
**After editing `.env`:** run `docker compose up -d --force-recreate api worker` so both services read the new values.

> **macOS:** if `make` complains about the Xcode licence, run `sudo xcodebuild -license` once, or use the `docker compose` commands above.

### Configuration

All settings live in `.env` (see [.env.example](.env.example)):

| Setting | Default | What it does |
|---|---|---|
| `CREDENTIALS_KEY` | generated | Encrypts saved connection passwords. Keep it secret and stable. |
| `APP_DB_PASSWORD`, `SCRATCH_DB_PASSWORD` | generated | Passwords of the app's own internal databases. |
| `ORACLE_CLIENT_MODE` | `thin` | Set to `thick` if your Oracle server enforces Native Network Encryption. |
| `TARGET_PG_VERSION` | `18` | PostgreSQL version Ora2Pg converts for. |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID` | empty | Enables the Claude reviews. |
| `REVIEW_MODEL`, `REVIEW_EFFORT`, `REVIEW_CONCURRENCY` | `claude-opus-5-5`, `high`, `4` | Model, effort and parallel requests for Claude reviews. |
| `PERF_REVIEW_MAX_OBJECTS`, `PERF_REVIEW_BATCH_OBJECTS`, `PERF_REVIEW_BATCH_CHARS` | `300`, `8`, `60000` | Limits for the Schema explorer's performance review. |
| `ORA2PG_REPORT_TIMEOUT_SECONDS` | `900` | Time allowed per schema for Ora2Pg's cost estimate during a scan. Large schemas can need `3600`. |
| `COMPILE_IN_SCRATCH` | `true` | Run `plpgsql_check` on a structure-only copy when the target doesn't have the extension. |

## How to use it

### 1. Add your connections

Open **Connections → New connection** and add your Oracle database (host, port, service name, username, password) and your PostgreSQL database (host, port, **database name**, username, password). Click **Test** on each.
- The Oracle test reports whether the account can read the `DBA_*` dictionary views. Without them only objects granted to the account are visible, and scans are slower and incomplete.
- For PostgreSQL, make sure the database is the one holding the migrated schemas, not the server's default `postgres` database.
- **Schemas in scope:** list the schemas the application should work with. Every feature then uses only those: scans, the comparison, the Schema explorer, Table compare, the Overview statistics, the sequence check and the AI reviews. This matters with an admin account that can see every schema. A project or page can narrow it further, but never go outside it. Leave it empty to use every user schema. After changing it, use **Refresh catalog**, and re-run scans, so existing results follow the new scope.
- The containers resolve hosts themselves: for a database running on your own machine use `host.docker.internal`, not `localhost`.

### Database statistics on the Overview page

Once a connection's catalog has been read (Schema explorer, or **Read catalog** on its Overview card), the Overview page shows its counts. Oracle package members are counted as procedures and functions too, with the split shown (e.g. "388 standalone · 570 in 139 packages").

**Oracle vs PostgreSQL differences**, below the cards, compares an Oracle catalog with a PostgreSQL one area by area: tables, views, columns, indexes, foreign keys, procedures, functions and triggers. For each area it shows how many match, how many exist **only in Oracle** (missing in PostgreSQL) and **only in PostgreSQL**. Click a count to list those items, filter them, download them as CSV, or open them in the Schema explorer or Table compare.
- **Matching rules:** columns, indexes and foreign keys are compared inside the tables that exist on both sides. Procedures and functions are matched across the migration (`PKG.MEMBER` ↔ `pkg.member`); one that changed kind, such as a procedure that became a function, counts as matched and is listed separately.
- **PostgreSQL scope for packages:** Ora2Pg moves each Oracle package into a PostgreSQL schema of the same name, so a PostgreSQL connection's scope must include those schemas. **Add schemas for Oracle packages** on its edit form adds them. Click any count, for example **Indexes** or **Foreign keys → without an index**, to list those items below. Narrow the list by schema, filter or search, and click a name to open it in the Schema explorer. **Refresh** on a card reads the catalog again.

**LOB data check**, further down, finds LOB data that didn't migrate. Choose the Oracle and PostgreSQL connections and click **Check LOB data**:
- **What it reads:** every Oracle table in scope with BLOB, CLOB, NCLOB, BFILE, LONG, LONG RAW or XMLTYPE columns, paired with its PostgreSQL table. On both sides it counts:
  - the rows;
  - per LOB column, the values that are not NULL and the values with data (length > 0);
  - optionally (**Measure sizes**), the total and largest length: bytes for binary, characters for text.
- **Highlighted:** tables or columns missing in PostgreSQL, rows missing, LOB values missing or NULL, and values that got shorter. Values cut at exactly 1,047,552 (Ora2Pg's default `LONGREADLEN`, with `LONGTRUNCOK` on) are flagged even when Oracle couldn't be measured. Empty Oracle LOBs (`EMPTY_BLOB()`) that became NULL are noted, not counted as missing.
- **Reading the results:** problems come first; filter by status or schema, search, and download the list as CSV. PostgreSQL tables with binary columns that don't exist in Oracle are listed as notes.
- **Load and time limit:** read-only, and only counts and lengths are read, never the contents. Every table is read in full, two at a time per database. A table that exceeds the per-table limit, or whose session is closed by the server, is counted again without sizes. Data changed after the migration also shows as a difference.

### 2. Assess the Oracle code (Scenario 1)

1. **Projects → New project**: pick the Oracle connection (and the PostgreSQL one, if a migration already exists). Optionally list the Oracle schemas to include; empty means every application schema.
2. Open the project and click **Run scan**. Tick *Ora2Pg cost estimate* for Ora2Pg's effort figures; it is the slow part on big schemas. Progress appears on the page and under **Jobs**.
3. Read the **Report** tab (size, effort, complexity, risky constructs, Oracle packages used), then drill into the **Inventory** and any object's page, which shows its source with the flagged lines.

### 3. Validate an existing migration (Scenario 2)

1. Give the project its PostgreSQL connection (and optionally *PostgreSQL schemas to compare*).
2. On the **Comparison with PostgreSQL** tab click **Run comparison**. Every Oracle routine, view and trigger is paired with its PostgreSQL version and checked.
3. Work through the pairs by risk. Each pair page shows Oracle and PostgreSQL side by side with the findings marked on both. If a pairing is wrong, fix it there; manual matches are kept on re-runs.
4. With an API key, use **Review with Claude…** on the risky pairs. An estimate of the cost is shown before anything is sent.

### 4. Explore a database

Open **Schema explorer**, choose a connection, and click **Read catalog** (seconds to a minute). Then:
- **Tables & views:** pick a table to see its relationships diagram, columns (and which are indexed), indexes (with foreign keys that lack one), the procedures and functions that use it (and how: SELECT/INSERT/UPDATE/DELETE), and its triggers.
- **Code:** search procedures, functions, packages and triggers by name or inside their source.
- **Performance review (AI):** on a table, have Claude point out which of the code using it needs performance work and suggest the fix (with an estimate first).

Click **Refresh catalog** after changes to the database.

### 5. Compare Oracle and PostgreSQL table by table

Open **Table compare**. Oracle is on the left and PostgreSQL on the right (change either with its dropdown). Both catalogs must have been read in the Schema explorer.
- The **All tables** list compares every table at once. Start with **With problems**, narrow by area (columns, indexes, triggers, routines…), and click a row.
- Or pick a table on either side: the same table is selected on the other side, or an error says it doesn't exist there.
- Each section lines both tables up with differences highlighted: **Problem** (likely to break or lose data), **Differs**, **Info**, **Equivalent**, **Same**. Click a trigger or routine to see both sources.

### 6. Fix sequences after loading data

Open **Sequence check**, choose the PostgreSQL connection (optionally the Oracle one to compare sequence values) and click **Check sequences**. Sequences behind the data are listed with the column they fill. Download the **fix script** and run it with a login that owns the sequences:

```bash
psql -h <host> -p <port> -d <database> -U <sequence owner> -f fix_sequences.sql
```

The script is safe to run again after every data load. To fix sequences without the app, use [scripts/postgres/fix_sequences.sql](scripts/postgres/fix_sequences.sql) (see [Sequence check](#sequence-check-after-loading-data)).

### Try it without your own databases

The `sandbox` profile adds a local Oracle Free with a sample schema full of hard-to-convert constructs, and an "already migrated" PostgreSQL copy with deliberately planted bugs:

```bash
make sandbox     # or: docker compose --profile sandbox up -d --build
```

The first start takes a minute or two. Then add these connections:

| | Oracle sandbox | PostgreSQL sandbox |
|---|---|---|
| Host / Port | `oracle-sandbox` / `1521` | `postgres-sandbox` / `5432` |
| Service / Database | `FREEPDB1` | `migrated` |
| Username / Password | `studio_reader` / `Sandbox_Reader_1` | `studio_reader` / `Sandbox_Reader_1` |

The sample schema (`MIGDEMO`, see [01_sample_schema.sql](docker/oracle-sandbox/init/01_sample_schema.sql)) contains:
- a `(+)` outer join
- `ROWNUM`
- `DECODE`
- `BULK COLLECT` / `FORALL`
- `COMMIT` inside a procedure
- an autonomous transaction
- package state
- `RAISE_APPLICATION_ERROR`
- a trigger using `:OLD`/`:NEW`
- Oracle's `'' = NULL` semantics

The sandbox passwords are for local use only.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Connection test times out | The containers can't reach the host: check VPN, firewalls, and use `host.docker.internal` for databases on your machine. |
| Oracle test says "no DBA_* access"; scans are slow or find little | The account only sees granted objects through `ALL_*` views. Grant `SELECT ANY DICTIONARY` (or `SELECT_CATALOG_ROLE`) and test again. |
| "Ora2Pg cost estimate failed … timed out" on a scan | Large schemas need longer: raise `ORA2PG_REPORT_TIMEOUT_SECONDS` (e.g. `3600`), recreate `api`/`worker`, and scan again. The scan itself still succeeds, using the app's own effort estimate for those schemas. |
| `DPY-3001` or `ORA-12660` (encryption) errors | The server requires Native Network Encryption, which thin mode doesn't support: set `ORACLE_CLIENT_MODE=thick` and recreate `api`/`worker`. |
| Sequence check: "No Oracle sequences found in schemas named like the PostgreSQL ones" | The PostgreSQL connection probably points at the wrong database (for example `postgres`). Edit the connection's database name. |
| Claude features say the API key is missing | Set `ANTHROPIC_API_KEY` in `.env` and run `docker compose up -d --force-recreate api worker`. |
| LOB data check: a table is "counted without sizes" or "took longer than the time limit" | Measuring LOB lengths reads every LOB's locator, which is slow on very large tables. Choose a longer **Limit per table**, or untick **Measure sizes** for a quicker row and value count. |
| A job seems stuck | Open it under **Jobs** to see its last progress message and log. Jobs interrupted by a worker restart are marked failed; start them again. |

## Architecture

```
ui (React + Vite, :5173) ──/api──▶ api (FastAPI, :8000) ──▶ app-db (Postgres 16: connections, projects, jobs, results)
                                        │
                                        ▼ Redis queue
                                    worker (Celery) ──▶ ora2pg ─ DBD::Oracle ─ Instant Client ──▶ Oracle
                                        │          └──▶ your Oracle and PostgreSQL (read-only sessions)
                                        └──▶ scratch-pg (Postgres 18 + plpgsql_check + orafce, the compile gate)
```

| Service | Purpose |
|---|---|
| `api` | REST API. Runs database migrations on start. |
| `worker` | Runs long jobs (Ora2Pg, scans, comparisons, catalog reads, reviews). Same image as `api`. |
| `app-db` | The application's own database. |
| `redis` | Job queue. |
| `scratch-pg` | Throwaway PostgreSQL for compile-checking code. Holds no real data. |
| `ui` | Web UI. |
| `oracle-sandbox` | *Optional* local Oracle with sample PL/SQL (`--profile sandbox`). |
| `postgres-sandbox` | *Optional* "already migrated" copy of the sample, with planted bugs (`--profile sandbox`). |

The backend image bundles **Ora2Pg 25.0**, **DBD::Oracle 1.95**, **Oracle Instant Client** 23.x (which connects to 19c) and **python-oracledb**.

## Feature reference

Details of what each part of the application does and how.

### Scanning and the assessment report

Open a project and click **Run scan**. A background job does the following:

1. **Reads the Oracle data dictionary** with bulk queries (one per view, filtered by owner): `DBA_OBJECTS`, `DBA_SOURCE`, `DBA_VIEWS`/`DBA_MVIEWS`, `DBA_TRIGGERS`, `DBA_PROCEDURES`/`DBA_ARGUMENTS`, `DBA_DEPENDENCIES` and `DBA_TABLES`. It falls back to the `ALL_*` views when the account has no `DBA_*` access, and shows a warning when it does. Leave a project's schema list empty to scan every non-Oracle-maintained schema that owns PL/SQL or views.
2. **Runs Ora2Pg's assessment** (`SHOW_REPORT --estimate_cost`) for each schema, and parses per-object and per-package-member costs from it. This step is optional because it's the slow part on large schemas. If you skip it, objects whose source hasn't changed keep their previous Ora2Pg cost.
3. **Analyses every object** against a rule catalogue (`GET /api/rules`, defined in [rules.py](backend/app/scan/rules.py)). Comments and string literals are masked first, so a `COMMIT` mentioned in a comment doesn't count. Findings fall into four categories:

   | Category | Question it answers | Examples |
   |---|---|---|
   | Execution safety | Can Scenario 2 run this inside a rolled-back test transaction? | `COMMIT`, autonomous transactions, `UTL_FILE`/`UTL_HTTP`, DB links, DDL in dynamic SQL, **calling** any of these |
   | Silent behaviour risk | Will it convert cleanly but return different results? | `(+)` joins, `SELECT INTO` without `STRICT`, `NO_DATA_FOUND` handlers, `''` as NULL, `ROWNUM`, `DECODE` NULL matching, date arithmetic |
   | Conversion effort | What needs rewriting? | `CONNECT BY`, pipelined functions, `GOTO`, package state, collections, `BULK COLLECT`/`FORALL`, dynamic SQL |
   | Blockers | Can it be converted at all? | Wrapped source, objects invalid in Oracle |

   "Unsafe to auto-run" **propagates through dependencies**. A procedure that calls an autonomous-transaction logger is unsafe too. Calls go through a package's spec, so a package with any unsafe member taints its callers (a conservative choice).
4. **Upserts the inventory** in one transaction, keyed by owner, type and name. Object and routine IDs stay stable across re-scans. Each scan records what was added, which sources changed, and which objects were removed from Oracle.

The **report** shows the inventory size, callable routines (standalone and package members), estimated effort, unsafe objects, blockers, a complexity distribution, a findings table, the highest-effort objects and the Oracle-supplied packages used. Every row links to the **inventory**, which is filterable, sortable and exportable to CSV. Each **object page** shows the source with flagged lines, findings with jump-to-line, routine signatures, and dependencies in both directions.

Effort uses Ora2Pg cost units, where 1 unit ≈ 5 minutes. Objects without an Ora2Pg cost (package specs, types, or scans without the estimate) use a heuristic based on size and findings, and are marked `*`.

### Scenario 2: checking an existing migration

Give the project its migrated PostgreSQL database as the target connection (a read-only role is enough; `pg_read_all_data` works), then open **Comparison with PostgreSQL** and click **Run comparison**. A background job does the following:

1. **Reads the PostgreSQL catalog** in a read-only session: functions, procedures, views, materialized views and triggers, with their definitions and argument lists. Objects owned by extensions (for example orafce's own functions) are skipped. To limit the comparison to particular schemas, set `"target_schemas": ["app", "billing"]` in the project settings.
2. **Pairs every Oracle unit** with its counterpart. A unit is a standalone procedure or function, a public package member, a view, or a trigger. The naming convention of the earlier migration is detected rather than assumed: it tries Ora2Pg's package-to-schema mapping (`pkg.member`), `pkg_member`, same schema and name, `public`, and any schema. Each candidate is scored on how well its parameter list matches. Overloads go to the closest signature, and no Postgres object is claimed twice. Low-confidence matches show their percentage. **Manual matches** (and "ignore") made on a pair's page survive every re-run.
3. **Compares signatures**: parameter count, order (swapped parameters are flagged), modes, types (for example `NUMBER`→`integer` truncates fractions, `DATE`→`date` drops the time of day), defaults, function vs procedure, and return types.
4. **Runs static pitfall checks** on each pair (see [static_checks.py](backend/app/compare/static_checks.py)):
   - a dead `NO_DATA_FOUND` handler, or `SELECT INTO` without `STRICT`;
   - a `(+)` outer join that became an inner join;
   - a lost `ROWNUM` limit;
   - `''`-as-NULL differences;
   - `CASE x WHEN NULL`;
   - lost autonomous transactions;
   - `COMMIT` inside a function;
   - leftover Oracle syntax (treated as low severity where orafce provides it);
   - lost error codes, transaction-time `now()`, and timestamp ± number;
   - trigger timing and event differences.

   It also flags callers of routines whose own migration is high risk, and runs **`plpgsql_check`**, which only analyses code statically and never executes it:
   - **Target has `plpgsql_check` installed:** it runs there directly.
   - **Target doesn't have it (the default case):** the app copies the target's *structure*, never its data, into a temporary database on the local `scratch-pg` and runs the check there. The copy takes about 10 bulk catalog queries in a read-only session (extensions, schemas, types, sequences, tables with columns and unique keys, views, routines, triggers), so it takes seconds even over a slow VPN. It skips partitions, uses the target's effective `search_path` so unqualified names resolve the same way, and the temporary database is dropped afterwards. Nothing is written to the target. `scratch-pg` includes `orafce`, `pg_partman`, `pg_similarity` and `pgtt` so copies of databases that use them load completely; other missing extensions are reported as warnings. Disable this with `COMPILE_IN_SCRATCH=false`.
   - **Temporary tables:** when the code creates a table at run time (`CREATE TEMP TABLE`), references to it are reported as low-severity notes rather than errors, since no static check can see them.
   - **Search path caveat:** results reflect the `search_path` of the login you connected with. If the application's own login sets a different one, some "does not exist" errors may not occur in production.
   - **Choosing schemas:** set **PostgreSQL schemas to compare** on the project to limit what is read.
5. **Records the outcome per pair**: risk (high, medium, low or none), signature status, findings with line numbers on both sides, Postgres objects with no Oracle counterpart, and a team review status with notes.

**Claude review** ("Review with Claude…") sends each pair's Oracle code (the whole package body for members, so package state is visible), the PostgreSQL code and the automated findings to Claude. It asks for a structured verdict (`equivalent`, `differences_found` or `needs_human_review`) with issues that cite lines on both sides and suggest fixes.

- **Setup:** set `ANTHROPIC_API_KEY` in `.env`, then run `docker compose up -d api worker`.
- **Model:** the default is `claude-opus-5-5` with adaptive thinking at `REVIEW_EFFORT=high`. `REVIEW_MODEL` can point at a cheaper model such as `claude-sonnet-5-5`.
- **Cost:** you see an estimate before every run. Pairs from the same package are reviewed back to back so the cached package body is reused.
- **Data:** only source code and findings are sent, never table data.
- **Refusals:** server-side fallbacks are enabled, and a refused review becomes "needs human review" instead of an error.
- **Staleness:** reviews are marked *stale* when either side's code changes.

The pair page shows Oracle and PostgreSQL side by side. The Oracle package body is dimmed outside the member under review, flagged lines are marked in both panes, and clicking a line reference jumps to it.

The sandbox profile includes `postgres-sandbox`, a hand-made "already migrated" copy of the sample schema with **planted bugs** to try this on. Connect to it with host `postgres-sandbox`, database `migrated`, user `studio_reader`, password `Sandbox_Reader_1`. The planted bugs are:
- a dropped `(+)` join;
- a dead `NO_DATA_FOUND` handler;
- a lost autonomous transaction;
- swapped parameters;
- `NUMBER` narrowed to `integer`;
- `timestamp - integer`;
- `''` handling;
- a function that was never migrated.

The integration test `test_scenario2_finds_the_planted_migration_bugs` asserts that each of them is reported.

### Schema explorer

**Schema explorer** (left menu) browses any saved connection, Oracle or PostgreSQL. Pick a connection and optionally limit the schemas (empty means every user schema), then click **Read catalog**. A background job reads the catalog in a read-only session and stores a snapshot in the app database. Only the latest snapshot per connection is kept; **Refresh catalog** replaces it.

Each table and view shows:
- **Relationships:** a foreign-key diagram (tables that reference it on the left, tables it references on the right; click a box to move there), the constraint columns and `ON DELETE` rules, and the views that read it.
- **Columns:** type, nullability, default and comment, with PK / FK / UQ markers. **Indexed in** lists every index that covers each column: its position in the key, `INCLUDE`, or inside an expression. A toggle shows only indexed columns.
- **Indexes:** each index's key columns in order, `INCLUDE` columns, unique/primary, method, partial-index `WHERE`, and unusable/invalid status. **Foreign keys without a supporting index** (no index starts with the FK columns) are flagged here, in the relationship table and in the table list. Without such an index, deleting or updating a parent row scans the child table, and Oracle also locks it.
- **Procedures & functions:** every procedure, function, package and trigger function that uses it.
  - How each one uses it (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, DDL, `%ROWTYPE`).
  - Which package members use it.
  - The source with those lines highlighted.
  - Filters for writers, readers and dynamic SQL.
- **Triggers:** timing, events, enabled state and source. On PostgreSQL this includes the trigger function, and triggers created on a partition are listed on its parent table.

- **Performance review (AI):** Claude reviews the procedures, functions and triggers that use the table and says which need performance work and what to change.
  - **Scope:** triggers on the table; triggers plus code that writes to it; or all code that uses it.
  - **Cost:** an estimate is shown before you run it.
  - **What is sent:** the source code, the structure of the tables that code touches (columns, keys, indexes, foreign keys and whether they are indexed) and the optimizer's row-count estimates. Never any table data.
  - **Results:** each object gets a priority, then issues with cited lines, a suggestion and an example rewrite. Table-level changes (mostly `CREATE INDEX`) are listed separately with copyable SQL.
  - **Refreshes:** results survive a catalog refresh. An object whose source has since changed is marked *changed since review*, with a button to re-review only those objects.
  - **Limits and settings:**
    - at most `PERF_REVIEW_MAX_OBJECTS` (default 300) objects per run;
    - objects are batched into requests of up to `PERF_REVIEW_BATCH_OBJECTS` objects or `PERF_REVIEW_BATCH_CHARS` characters of source;
    - batches run `REVIEW_CONCURRENCY` at a time, with the model and effort set by `REVIEW_MODEL` / `REVIEW_EFFORT`;
    - the shared structure block is prompt-cached across batches.

The list on the left has two modes:
- **Tables & views:** search, schema and kind filters, plus "has a foreign key without an index" and "has no index".
- **Code:** search procedures, functions, packages and triggers by name or signature. Tick **Also search inside the source code** to search the source as well; matches are highlighted in the source view. Filters cover schema, kind (procedure, function, trigger function, package, trigger), table access (writes, only reads, uses no tables) and, on Oracle, invalid objects.

Opening a code object shows its signature, the table a trigger fires on, every table it uses (operations, package members, lines, with a button to highlight them) and its source.

How code is linked to tables:
- **PostgreSQL** doesn't record what a function body references, so the source is read. Comments are ignored, and names resolve through the function's own schema, its `SET search_path`, then the session's `search_path`.
- **Oracle**'s dependency views (`DBA_`/`ALL_DEPENDENCIES`) are authoritative for static SQL; the source adds how each table is used.
- **Both:** table names inside string literals are reported as **dynamic SQL**. Tables built from variables (`'... ' || v_table`) can't be resolved.

PostgreSQL partitions are folded into their parent. With an Oracle account that only has `ALL_*` access, the snapshot lists only what that account has privileges on, and package bodies are usually hidden. The snapshot says so; granting `SELECT ANY DICTIONARY` fixes it.

### Table compare

**Table compare** (left menu) puts two connections side by side, usually Oracle on the left and the migrated PostgreSQL on the right. It also works for any two, such as a test and a production copy.
- **Picking a table:** search and pick a table on either half. The same `schema.table` is selected on the other half, ignoring upper/lower case. If the schema differs, it falls back to the only table with that name. If there is no such table, the other half says so and you can pick one manually.
- **Data source:** the comparison uses each connection's last catalog read (Schema explorer), so it doesn't query the databases again. Refresh a catalog to pick up changes.

Each section lines the two tables up row by row: half the page per database, with a status in the middle.

| Section | Compared |
|---|---|
| Columns and data types | Matched by name. Oracle→PostgreSQL types are judged by meaning, not spelling: `NUMBER(12)`→`bigint` is equivalent, `NUMBER(12)`→`integer` can overflow, `VARCHAR2(50)`→`varchar(30)` truncates, `DATE`→`date` drops the time of day. Also nullability, column order, and defaults (PostgreSQL vs PostgreSQL). |
| Keys | Primary and unique keys, by columns. |
| Indexes | By key columns: missing (an error if unique, a performance warning otherwise), uniqueness, partial conditions, invalid/unusable. |
| Foreign keys | Outgoing and incoming, by columns and referenced table; `ON DELETE` rules. |
| Triggers | By name (or by timing and events when renamed): timing, events, row/statement, enabled. |
| Procedures, functions and packages | Matched across the migration: an Oracle package member `PKG.MEMBER` matches PostgreSQL `pkg.member`, as Ora2Pg names it. Shows code that is missing, or that exists but no longer uses the table, and operation changes such as "no longer DELETE" (per package member). |
| Views | Views reading the table. |

**Stored procedure comparison:** below the routines list, each procedure and function that uses the selected table is compared with its counterpart:
- **Parameters:** matched by name when they have been reordered, then names, order, `IN`/`OUT` modes, types, defaults and return type. Conversions Ora2Pg makes on purpose (procedure `OUT` → `INOUT`, `NUMBER` parameters → `bigint`) are shown as notes rather than problems.
- **Tables used:** every table each version reads or writes, for example *no longer uses `app.audit_log`* or *no longer UPDATEs*. For Oracle packages this is per member.
- **Code checks:** the Oracle→PostgreSQL pitfall checks, such as a dead `NO_DATA_FOUND` handler, `SELECT INTO` without `STRICT`, a `(+)` join that became an inner join, or `''` compared as NULL.

Choose **By routine** to expand a pair: its differences, the parameters side by side, and both sources with the lines marked (only the member's lines for a package member). Choose **List differences** for every difference in one table, filterable by severity, kind and name, and downloadable as CSV.

**All tables:** above the comparison, every matching pair of tables is compared at once (under a second for about 2,000 pairs; cached per pair of catalog reads), so you can go straight to the tables that need attention. Filter by:
- *with problems*, *with differences*, *only in Oracle*, *only in PostgreSQL*, or *matching*;
- area (columns, indexes, keys, foreign keys, triggers, routines, views);
- name.

Each row shows where the issues are and the first one. Click a row to open that table side by side. Each table picker also has an option to list only tables with problems or differences.

Statuses: **Problem** (likely to break or lose data), **Differs** (may be intended), **Info**, **Equivalent** (same meaning), **Same**. Problems sort first. "Show only differences" hides the rest. Click a trigger or routine row to see both sources side by side.

### Sequence check (after loading data)

Data loaded from Oracle keeps its key values, but the PostgreSQL sequences that generate new keys don't move with it. The next insert then fails with *duplicate key value violates unique constraint*. The fix is to set each sequence past the highest value already in the column it fills. The hard part is knowing which column that is: code converted from Oracle rarely uses column defaults.

**Sequence check** (left menu) works this out for a PostgreSQL connection:
- **Finds the columns each sequence fills,** from:
  - identity and serial columns;
  - column `DEFAULT nextval(...)`;
  - triggers that assign `NEW.col := nextval(...)` or `SELECT nextval(...) INTO NEW.col`;
  - `INSERT INTO t (cols) VALUES/SELECT (... nextval(...) ...)` statements in procedures and functions (the value is matched to its column).
- **Reads values:** the current `max()` of each linked column, and each sequence's own state.
- **Compares with Oracle (optional):** with an Oracle connection, it compares with Oracle's `LAST_NUMBER` for the sequence of the same schema and name, so values Oracle already handed out (to files, other systems) aren't reissued.
- **Status for each sequence:**
  - *behind the data* (the next insert collides);
  - *behind Oracle*;
  - *too small* (an `integer` sequence that must pass 2³¹ − 1);
  - *not linked* (no column found: application code may use it);
  - *OK*.
- **Generates a fix script.** The app's sessions are read-only, so the fix is a SQL script you run, either for the sequences found behind or as a re-sync of every linked sequence. Each statement recomputes `max()` when it runs, includes Oracle's value, and never moves a sequence backwards (`GREATEST(..., its own next value)`), so it is safe to re-run after every load:

  ```bash
  psql -h <host> -p <port> -d <database> -U <sequence owner> -f fix_sequences.sql
  ```

**Sequence statistics** (left menu) looks at sequences schema by schema, on Oracle, PostgreSQL or both (choose either or both connections, then **Analyse sequences**):
- **By schema:** how many sequences each schema has, and whether each one fills tables of its own schema, **fills tables of another schema** (flagged, e.g. a sequence in SALES used for a BILLING table), is only used by code, or is unused. Click a schema to list its sequences.
- **Finding the tables a sequence fills on Oracle:**
  - identity columns;
  - triggers that set `:NEW.ID := SEQ.NEXTVAL` or run `SELECT SEQ.NEXTVAL INTO :NEW.ID FROM DUAL`;
  - `INSERT … VALUES (SEQ.NEXTVAL, …)` in procedures and packages;
  - Oracle's dependency views, for code that uses a sequence.

  PostgreSQL is analysed as in the Sequence check.
- **Table by table** (with both connections): for every column filled by a sequence, the Oracle sequence's `LAST_NUMBER` and `MAX()` of the data next to the PostgreSQL sequence's next value and `MAX()`. Highlighted when:
  - the PostgreSQL sequence is behind the data (next insert fails);
  - it is behind Oracle's value;
  - no sequence fills the column in PostgreSQL;
  - PostgreSQL holds less data than Oracle.

  It can be downloaded as CSV, along with a fix script for the PostgreSQL sequences.
- **Not linked to a column:** Oracle sequences no column was found for, compared with PostgreSQL by name.

**Without the app:** [scripts/postgres/fix_sequences.sql](scripts/postgres/fix_sequences.sql) does the same directly in psql for identity/serial columns, defaults and trigger-filled columns. It doesn't cover INSERT statements in procedures; use the check page for those. Dry run first (it only reports), then apply:

```bash
psql -h <host> -d <db> -U <sequence owner> -v schemas='sales,billing' -v apply=off -f scripts/postgres/fix_sequences.sql
psql -h <host> -d <db> -U <sequence owner> -v schemas='sales,billing' -v apply=on -f scripts/postgres/fix_sequences.sql
```

## Connecting your Oracle 19c

Use a **read-only account**. Scanning needs no write access:

```sql
CREATE USER studio_reader IDENTIFIED BY "<strong password>";
GRANT CREATE SESSION, SELECT ANY DICTIONARY TO studio_reader;  -- or SELECT_CATALOG_ROLE
```

The connection test reports whether the account can read the `DBA_*` views. If it can't, Ora2Pg falls back to `ALL_*` views (`USER_GRANTS 1`), which only show objects granted to that account, so a scan could silently miss objects.

If your server enforces **Native Network Encryption**, set `ORACLE_CLIENT_MODE=thick` in `.env` and restart. Thin mode doesn't support NNE.

The `api` and `worker` containers must be able to reach the database host. Inside Docker, `localhost` is the container itself; use `host.docker.internal` for a database on your machine.

## Security notes

- **Encrypted passwords.** Connection passwords are encrypted with Fernet (`CREDENTIALS_KEY` in `.env`) and are never returned by the API. **Back up `.env`**: losing or changing the key makes saved passwords unreadable.
- **Password stays out of files and process lists.** Ora2Pg gets the Oracle password through the `ORA2PG_PASSWD` environment variable. Generated `ora2pg.conf` files never contain it, are created with mode `0600` in a private directory, and are deleted after each run.
- **Read-only sessions to your databases.** The app never writes to the Oracle or PostgreSQL databases you connect; it only writes to its own `app-db`.
  - **PostgreSQL:** every session starts with `default_transaction_read_only=on`, so all writes fail, including DDL, temp tables and `DO` blocks. If a connection pooler rejects startup options, connect to the database directly.
  - **Oracle:** 19c has no read-only session mode, so there are two layers.
    - Each session starts with `SET TRANSACTION READ ONLY`, so DML fails with ORA-01456.
    - Every statement passes through a guard that only sends `SELECT`/`WITH` queries. That's needed because DDL commits implicitly and ends the read-only transaction, PL/SQL blocks can run autonomous transactions, and a `COMMIT` would make the session read-write again. The guard refuses those, along with `commit()` and procedure calls.
  - **Ora2Pg:** it only runs its report modes (`SHOW_VERSION`, `SHOW_REPORT`), with `TRANSACTION readonly`.
  - **Never in the way of other sessions:** every PostgreSQL session also sets `lock_timeout = 5s`, so a read that meets a table locked by DDL or maintenance gives up quickly instead of queueing behind it, where it could delay that work. The catalog read takes no table locks for its main queries. For a table that another session holds exclusively, it skips only the parts PostgreSQL can't produce without a lock (column defaults, full index and trigger definitions) and says so in the snapshot's warnings.
  - **Still use read-only accounts.** A database account without write privileges is the only guarantee that also covers other tools and future code.
- **Validated connection fields.** Host, service and user names are restricted to safe characters, because they end up inside Ora2Pg's config file and DBI DSN.
- **Local-only ports.** All ports are bound to `127.0.0.1`. There is no authentication yet, so don't expose the stack beyond your machine.

## Development

```bash
make test                     # backend tests (creates the studio_test database on first run)
make logs                     # follow logs
make revision m="add scans"   # autogenerate an Alembic migration
```

The source is bind-mounted, so the API and UI reload on save. Restart the worker after changing job code: `docker compose restart worker`.

Oracle integration tests run when `ORACLE_TEST_*` is set:

```bash
docker compose exec -e ORACLE_TEST_HOST=oracle-sandbox -e ORACLE_TEST_SERVICE=FREEPDB1 \
  -e ORACLE_TEST_USER=studio_reader -e ORACLE_TEST_PASSWORD=Sandbox_Reader_1 \
  -e PG_TARGET_TEST_HOST=postgres-sandbox -e PG_TARGET_TEST_DB=migrated \
  -e PG_TARGET_TEST_USER=studio_reader -e PG_TARGET_TEST_PASSWORD=Sandbox_Reader_1 api pytest -q
```

### Layout

```
backend/
  app/
    api/          REST routers (connections, projects, scans, comparison, catalog, jobs, system)
    jobs/         job framework (registry.py) and handlers (handlers.py)
    models/       SQLAlchemy models (inventory.py: scans, objects, routines, dependencies)
    scan/         extract.py (Oracle dictionary), plsql.py (masking), rules.py (findings),
                  cost_report.py (Ora2Pg), service.py (scan + persist), report.py
    compare/      target_extract.py (PG catalog), units.py, pairing.py, signatures.py,
                  static_checks.py, service.py (comparison run), review.py + review_service.py (Claude)
    sequences/    sequence check: parse.py (nextval -> column), check.py, script.py (fix SQL)
    catalog/      table_compare.py (side-by-side table comparison), schema explorer: oracle_reader.py / postgres_reader.py, references.py
                  (code -> table links), service.py (snapshot job + persist)
    services/     oracle.py / postgres.py probes, ora2pg.py CLI wrapper
    schemas.py    API request/response models
    worker.py     Celery app
  alembic/        migrations
  tests/
docker/
  scratch-pg/     compile-gate Postgres image
  oracle-sandbox/ sample schema for local testing
ui/src/           React app
```

### Adding a background job

Register a handler and it can be queued with `jobs.create_job(...)` + `jobs.enqueue(...)`:

```python
@job_handler("scan")
def scan(ctx: JobContext) -> dict:
    ctx.progress(0.1, "Reading DBA_OBJECTS")
    ctx.log("...")
    return {"objects": 912}   # stored on the job row
```

## Roadmap

1. ~~**Foundation**: stack, connections, projects, jobs, Ora2Pg toolchain~~
2. ~~**Scan & report**: inventory with dependencies and routine signatures, Ora2Pg cost estimates, risk findings, transitive "unsafe to auto-execute" flags, report, inventory and object views, CSV export~~
3. ~~**Scenario 2 (static)**: pairing with detected naming conventions and manual overrides, signature comparison, pitfall checks, plpgsql_check, Claude review, side-by-side pair view~~
4. **Scenario 2 (execution)**: generate, accept or capture test inputs; run both sides in rolled-back transactions; compare results; compare performance (license-free by default: `V$SQL` / `DBMS_XPLAN` / timing)
5. **Scenario 1**: bulk Ora2Pg conversion, the `plpgsql_check` compile gate, a Claude fix loop, side-by-side review and export
