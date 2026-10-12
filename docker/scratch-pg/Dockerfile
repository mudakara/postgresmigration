# Throwaway Postgres used as the compile gate: converted code is created here inside a
# transaction, checked with plpgsql_check, then rolled back. Keep the major version equal
# to the migration target (TARGET_PG_VERSION). The extra extensions exist so schema copies
# of targets that use them load completely (otherwise plpgsql_check reports their
# functions and types as missing).
FROM postgres:18-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-18-plpgsql-check postgresql-18-orafce \
      postgresql-18-partman postgresql-18-similarity postgresql-18-pgtt \
 && rm -rf /var/lib/apt/lists/*

COPY init.sql /docker-entrypoint-initdb.d/10-extensions.sql
