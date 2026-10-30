"""PostgreSQL client of the Lambda SQL runtime: pg8000 (pure Python), vendored in the Lambda zip."""

from __future__ import annotations

from pathlib import Path

SQL_JOBS_DIR = Path(__file__).resolve().parent.parent / "lambdas" / "sql_jobs"
VENDOR_DIR = SQL_JOBS_DIR / "vendor"
# Amazon RDS global CA bundle, shipped in the zip and pinned (make vendor-check).
CA_BUNDLE = SQL_JOBS_DIR / "rds-global-bundle.pem"
CA_BUNDLE_SHA256 = "90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b"
