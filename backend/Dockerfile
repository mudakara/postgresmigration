# One image for both the API and the worker: both need python-oracledb, and the
# worker needs Ora2Pg (Perl + DBD::Oracle + Oracle Instant Client).
FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG ORA2PG_VERSION=25.0
ARG DBD_ORACLE_VERSION=1.95

ENV ORACLE_HOME=/opt/oracle/instantclient \
    LD_LIBRARY_PATH=/opt/oracle/instantclient \
    NLS_LANG=AMERICAN_AMERICA.AL32UTF8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      perl make gcc libc6-dev libaio1 libdbi-perl cpanminus curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Oracle Instant Client: "basic" for the runtime libraries, "sdk" for the headers
# DBD::Oracle compiles against. Instant Client 23 connects to Oracle 19c fine.
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) ic_arch=linuxx64 ;; \
      arm64) ic_arch=linux-arm64 ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt/oracle; cd /opt/oracle; \
    for pkg in basic sdk; do \
      curl -fsSLo ic.zip "https://download.oracle.com/otn_software/linux/instantclient/instantclient-${pkg}-${ic_arch}.zip"; \
      unzip -qo ic.zip; rm ic.zip; \
    done; \
    ln -s /opt/oracle/instantclient_* "$ORACLE_HOME"; \
    echo "$ORACLE_HOME" > /etc/ld.so.conf.d/oracle-instantclient.conf; \
    ldconfig

# DBD::Oracle tests need a live database, so skip them during the build.
RUN cpanm --notest "DBD::Oracle@${DBD_ORACLE_VERSION}" \
 && rm -rf /root/.cpanm

RUN set -eux; \
    curl -fsSL "https://github.com/darold/ora2pg/archive/refs/tags/v${ORA2PG_VERSION}.tar.gz" | tar -xz -C /tmp; \
    cd "/tmp/ora2pg-${ORA2PG_VERSION}"; \
    perl Makefile.PL; make; make install; \
    cd /; rm -rf "/tmp/ora2pg-${ORA2PG_VERSION}"; \
    ora2pg --version; \
    perl -MDBD::Oracle -e 'print "DBD::Oracle $DBD::Oracle::VERSION\n"'

WORKDIR /srv/backend

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

COPY . .

RUN useradd --create-home --uid 1000 studio \
 && mkdir -p /data/work \
 && chown -R studio:studio /data /srv/backend
USER studio

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
