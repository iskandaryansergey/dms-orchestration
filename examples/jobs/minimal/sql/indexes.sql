-- Secondary indexes after the full load. IF NOT EXISTS keeps the job safe to run twice.
CREATE INDEX IF NOT EXISTS accounts_customer_idx ON app.accounts (customer_id);
ANALYZE app.accounts;
