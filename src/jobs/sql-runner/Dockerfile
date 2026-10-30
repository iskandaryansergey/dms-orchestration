# sql-runner: Fargate image for long pre/reset/post/on_failure jobs.
# Tools: psql (PostgreSQL 17 client), mysql client, Python 3.13 + boto3, bash, PowerShell 7.
#
# Build from the repository root (the image needs src/shared/expr.py):
#   docker build -f src/jobs/sql-runner/Dockerfile -t sql-runner:<git-sha> .
# CI scans it with trivy (fails on HIGH/CRITICAL) and lints it with hadolint.

# Pin by digest in the target platform's registry mirror (CI resolves the tag to a digest).
FROM python:3.13.16-slim-bookworm

ARG POWERSHELL_VERSION=7.4.6
ARG INSTALL_POWERSHELL=true
ARG PG_MAJOR=17

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# PostgreSQL client from the PGDG repository (Aurora PostgreSQL 13-17), MySQL-compatible client,
# RDS CA bundle for sslmode=verify-full. libssl3/openssl: take the bookworm-security build
# (the base image can lag behind it; trivy fails on fixed HIGH/CRITICAL CVEs).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release libssl3 openssl \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}" default-mysql-client \
 && curl -fsSL -o /etc/ssl/certs/rds-global-bundle.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
 && if [ "${INSTALL_POWERSHELL}" = "true" ]; then \
      curl -fsSL -o /tmp/pwsh.deb "https://github.com/PowerShell/PowerShell/releases/download/v${POWERSHELL_VERSION}/powershell_${POWERSHELL_VERSION}-1.deb_amd64.deb" \
      && apt-get install -y --no-install-recommends /tmp/pwsh.deb \
      && rm -f /tmp/pwsh.deb; \
    fi \
 && apt-get purge -y --auto-remove gnupg lsb-release \
 && rm -rf /var/lib/apt/lists/*

COPY src/jobs/sql-runner/requirements.txt /opt/orch/requirements.txt
# pip is not needed at runtime: remove it with its vendored certifi, msgpack, urllib3 and
# setuptools (pkg_resources), which trivy reads from pip's SBOM.
RUN pip install --no-cache-dir -r /opt/orch/requirements.txt \
 && pip uninstall -y pip

COPY src/shared/__init__.py src/shared/expr.py src/shared/preflight.py src/shared/fingerprint.py src/shared/bundle.py src/shared/errors.py /opt/orch/shared/
COPY src/jobs/sql-runner/orch_job.py src/jobs/sql-runner/orch_sql.py src/jobs/sql-runner/orch_dump.py \
     src/jobs/sql-runner/orch_verify.py /opt/orch/
COPY src/jobs/sql-runner/bin/ /usr/local/bin/
RUN chmod 0755 /usr/local/bin/psql-run /usr/local/bin/mysql-run /usr/local/bin/orch-job \
      /usr/local/bin/pg-dump-s3 /usr/local/bin/pg-restore-s3 /usr/local/bin/verify-target \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin orch

# The task definition mounts writable volumes at /tmp, /work and /out; the root file system is read-only.
ENV PYTHONPATH=/opt/orch \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORCH_OUT_DIR=/out \
    ORCH_WORK_DIR=/work \
    ORCH_TMP_DIR=/tmp/orch

USER 10001
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/orch-job"]
CMD ["bash", "-c", "echo 'sql-runner: pass the job command as arguments'; exit 2"]
