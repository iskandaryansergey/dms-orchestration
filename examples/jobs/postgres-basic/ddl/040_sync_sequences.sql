-- DMS copies rows, not sequence positions. Create one sequence per id column and move it
-- past the loaded maximum so new rows on the target do not collide. Idempotent.
DO $$
DECLARE
    t record;
    next_value bigint;
BEGIN
    FOR t IN SELECT * FROM (VALUES
        ('customers', 'customer_id'),
        ('accounts', 'account_id'),
        ('transactions', 'transaction_id')
    ) AS v(tbl, col)
    LOOP
        EXECUTE format('CREATE SEQUENCE IF NOT EXISTS app.%I OWNED BY app.%I.%I', t.tbl || '_' || t.col || '_seq', t.tbl, t.col);
        EXECUTE format('SELECT coalesce(max(%I), 0) + 1 FROM app.%I', t.col, t.tbl) INTO next_value;
        EXECUTE format('SELECT setval(%L, %s, false)', 'app.' || t.tbl || '_' || t.col || '_seq', next_value);
        EXECUTE format('ALTER TABLE app.%I ALTER COLUMN %I SET DEFAULT nextval(%L)', t.tbl, t.col, 'app.' || t.tbl || '_' || t.col || '_seq');
    END LOOP;
END
$$;
