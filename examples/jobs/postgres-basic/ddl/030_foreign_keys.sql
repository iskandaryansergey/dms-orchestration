-- Foreign keys after the load. NOT VALID + VALIDATE keeps locks short on big tables.
-- The DO blocks make the script idempotent (constraints are added only once).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'accounts_customer_fk') THEN
        ALTER TABLE app.accounts
            ADD CONSTRAINT accounts_customer_fk FOREIGN KEY (customer_id)
            REFERENCES app.customers (customer_id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'transactions_account_fk') THEN
        ALTER TABLE app.transactions
            ADD CONSTRAINT transactions_account_fk FOREIGN KEY (account_id)
            REFERENCES app.accounts (account_id) NOT VALID;
    END IF;
END
$$;

ALTER TABLE app.accounts VALIDATE CONSTRAINT accounts_customer_fk;
ALTER TABLE app.transactions VALIDATE CONSTRAINT transactions_account_fk;
