# sql-runner: image for the optional Fargate runtime (ADR 0011). The module never builds it:
# the default path runs SQL jobs in Lambda and long jobs on SSM; whoever wants Fargate builds
# this image in their own pipeline and passes the pushed URI (by digest) in fargate.images.
# Tools: psql (PostgreSQL 17 client), mysql client, Python 3.13 + boto3, bash; PowerShell 7
# with --build-arg INSTALL_POWERSHELL=true.
#
# Build from the repository root (the image needs modules/dms-orchestration/src/shared/expr.py):
#   docker build -f modules/dms-orchestration/src/jobs/sql-runner/Dockerfile -t sql-runner:<git-sha> .
# Every source is a build arg, so a platform without internet access builds from its mirrors:
#   --build-arg BASE_IMAGE=<registry mirror>/python:<tag>@sha256:<digest>
#   --build-arg PGDG_APT_URL=... PGDG_KEY_URL=... PIP_INDEX_URL=... RDS_CA_URL=... POWERSHELL_DEB_URL=...
# Every download is checked (sha256 or the signing key fingerprint), Python packages install by
# hash only (requirements.txt, compiled from requirements.in). Mirrors serve the same bytes,
# so the checksums stay. CI (only when vars.BUILD_SQL_RUNNER_IMAGE is 'true') lints it with
# hadolint and scans it with trivy (fails on HIGH/CRITICAL).

# The digest of the official image's index; a mirror keeps it.
ARG BASE_IMAGE=python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d
FROM ${BASE_IMAGE}

ARG PG_MAJOR=17
ARG PGDG_APT_URL=https://apt.postgresql.org/pub/repos/apt
ARG PGDG_KEY_URL=https://www.postgresql.org/media/keys/ACCC4CF8.asc
ARG PGDG_KEY_FINGERPRINT=B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8
# The bundle the Lambda SQL runtime pins too (modules/dms-orchestration/src/shared/sqlclient.py CA_BUNDLE_SHA256).
ARG RDS_CA_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG INSTALL_POWERSHELL=false
ARG POWERSHELL_DEB_URL=https://github.com/PowerShell/PowerShell/releases/download/v7.4.6/powershell_7.4.6-1.deb_amd64.deb
ARG POWERSHELL_DEB_SHA256=79642721f0bc9baf07dafaab68ece1cbd822f86722492acf9b4031d41029a735

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# PostgreSQL client from the PGDG repository (Aurora PostgreSQL 13-17): its signing key is used
# only when its fingerprint is the pinned one and it is the only key in the file. MySQL-compatible
# client, RDS CA bundle for sslmode=verify-full (sha256 checked). libssl3/openssl: take the
# bookworm-security build (the base image can lag behind it; trivy fails on fixed HIGH/CRITICAL).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release libssl3 openssl \
 && GNUPGHOME="$(mktemp -d)" && export GNUPGHOME \
 && curl -fsSL -o /tmp/pgdg.asc "${PGDG_KEY_URL}" \
 && fprs="$(gpg --batch --show-keys --with-colons /tmp/pgdg.asc | awk -F: '$1 == "pub" {getline; print $10}')" \
 && test "${fprs}" = "${PGDG_KEY_FINGERPRINT}" \
 && install -d /usr/share/postgresql-common/pgdg \
 && install -m 0644 /tmp/pgdg.asc /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && rm -rf /tmp/pgdg.asc "${GNUPGHOME}" \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] ${PGDG_APT_URL} $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}" default-mysql-client \
 && curl -fsSL -o /etc/ssl/certs/rds-global-bundle.pem "${RDS_CA_URL}" \
 && echo "${RDS_CA_SHA256}  /etc/ssl/certs/rds-global-bundle.pem" | sha256sum -c - \
 && if [ "${INSTALL_POWERSHELL}" = "true" ]; then \
      curl -fsSL -o /tmp/pwsh.deb "${POWERSHELL_DEB_URL}" \
      && echo "${POWERSHELL_DEB_SHA256}  /tmp/pwsh.deb" | sha256sum -c - \
      && apt-get install -y --no-install-recommends /tmp/pwsh.deb \
      && rm -f /tmp/pwsh.deb; \
    fi \
 && apt-get purge -y --auto-remove gnupg lsb-release \
 && rm -rf /var/lib/apt/lists/*

COPY modules/dms-orchestration/src/jobs/sql-runner/requirements.txt /opt/orch/requirements.txt
# Wheels by hash only. pip is not needed at runtime: remove it with its vendored certifi,
# msgpack, urllib3 and setuptools (pkg_resources), which trivy reads from pip's SBOM.
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --index-url "${PIP_INDEX_URL}" -r /opt/orch/requirements.txt \
 && pip uninstall -y pip

COPY modules/dms-orchestration/src/shared/__init__.py modules/dms-orchestration/src/shared/expr.py modules/dms-orchestration/src/shared/preflight.py modules/dms-orchestration/src/shared/fingerprint.py modules/dms-orchestration/src/shared/bundle.py modules/dms-orchestration/src/shared/errors.py /opt/orch/shared/
COPY modules/dms-orchestration/src/jobs/sql-runner/orch_job.py modules/dms-orchestration/src/jobs/sql-runner/orch_sql.py modules/dms-orchestration/src/jobs/sql-runner/orch_dump.py \
     modules/dms-orchestration/src/jobs/sql-runner/orch_verify.py /opt/orch/
COPY modules/dms-orchestration/src/jobs/sql-runner/bin/ /usr/local/bin/
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
