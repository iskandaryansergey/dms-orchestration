-- Target tables for DMS full load (TargetTablePrepMode = DO_NOTHING).
-- Primary keys only: secondary indexes and foreign keys are created after the load
-- (020_indexes.sql, 030_foreign_keys.sql) so the load is faster and order-independent.
-- Idempotent: safe to run again.

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.customers (
    customer_id   bigint       PRIMARY KEY,
    email         text         NOT NULL,
    full_name     text         NOT NULL,
    created_at    timestamptz  NOT NULL
);

CREATE TABLE IF NOT EXISTS app.accounts (
    account_id    bigint         PRIMARY KEY,
    customer_id   bigint         NOT NULL,
    iban          varchar(34)    NOT NULL,
    currency      char(3)        NOT NULL,
    balance       numeric(18, 2) NOT NULL,
    opened_at     timestamptz    NOT NULL
);

CREATE TABLE IF NOT EXISTS app.transactions (
    transaction_id bigint         PRIMARY KEY,
    account_id     bigint         NOT NULL,
    amount         numeric(18, 2) NOT NULL,
    booked_at      timestamptz    NOT NULL,
    reference      text
);

-- No primary key on purpose: DMS full load works, CDC needs a PK or REPLICA IDENTITY FULL.
CREATE TABLE IF NOT EXISTS app.audit_notes (
    customer_id   bigint      NOT NULL,
    note          text        NOT NULL,   -- LOB-sized text
    written_at    timestamptz NOT NULL
);
