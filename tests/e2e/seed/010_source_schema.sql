-- Source database for the sandbox e2e scenarios. Run as the source master user.
-- Covers: tables with and without PK, LOB-sized text, sequences, foreign keys.
-- Users: dms_source (DMS endpoint) and migration_runner (pre/post jobs). Passwords are set
-- outside this file (ALTER ROLE ... PASSWORD from Secrets Manager); never commit them.

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.customers (
    customer_id bigserial PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.accounts (
    account_id  bigserial PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES app.customers (customer_id),
    iban        varchar(34) NOT NULL UNIQUE,
    currency    char(3) NOT NULL,
    balance     numeric(18, 2) NOT NULL DEFAULT 0,
    opened_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.transactions (
    transaction_id bigserial PRIMARY KEY,
    account_id     bigint NOT NULL REFERENCES app.accounts (account_id),
    amount         numeric(18, 2) NOT NULL,
    booked_at      timestamptz NOT NULL DEFAULT now(),
    reference      text
);

CREATE TABLE IF NOT EXISTS app.audit_notes (
    customer_id bigint NOT NULL,
    note        text NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dms_source') THEN
        CREATE ROLE dms_source LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_runner') THEN
        CREATE ROLE migration_runner LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA app TO dms_source, migration_runner;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO dms_source, migration_runner;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO dms_source, migration_runner;
-- CDC on Aurora PostgreSQL: GRANT rds_replication TO dms_source;  (and rds.logical_replication = 1)
