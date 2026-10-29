-- Secondary indexes after the load. IF NOT EXISTS keeps the job idempotent (safe to retry).
CREATE UNIQUE INDEX IF NOT EXISTS customers_email_uq ON app.customers (email);
CREATE INDEX IF NOT EXISTS accounts_customer_idx ON app.accounts (customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS accounts_iban_uq ON app.accounts (iban);
CREATE INDEX IF NOT EXISTS transactions_account_booked_idx ON app.transactions (account_id, booked_at);
CREATE INDEX IF NOT EXISTS audit_notes_customer_idx ON app.audit_notes (customer_id);
ANALYZE app.customers;
ANALYZE app.accounts;
ANALYZE app.transactions;
ANALYZE app.audit_notes;
