-- Deterministic test data: 10k customers, 20k accounts, 200k transactions, 5k LOB notes.
-- Sizes are small enough for a dms.t3.medium full load in a few minutes and big enough
-- to interrupt the load mid-way in scenario S2.
INSERT INTO app.customers (email, full_name, created_at)
SELECT 'customer' || g || '@example.test', 'Customer ' || g, timestamptz '2025-01-01' + g * interval '1 minute'
FROM generate_series(1, 10000) AS g
ON CONFLICT DO NOTHING;

INSERT INTO app.accounts (customer_id, iban, currency, balance, opened_at)
SELECT 1 + (g % 10000), 'GB00TEST' || lpad(g::text, 14, '0'), (ARRAY['GBP', 'EUR', 'USD'])[1 + g % 3],
       round((g * 13.37)::numeric % 100000, 2), timestamptz '2025-02-01' + g * interval '1 minute'
FROM generate_series(1, 20000) AS g
ON CONFLICT DO NOTHING;

INSERT INTO app.transactions (account_id, amount, booked_at, reference)
SELECT 1 + (g % 20000), round(((g % 1000) - 500)::numeric * 1.11, 2),
       timestamptz '2025-03-01' + g * interval '10 seconds', 'REF-' || g
FROM generate_series(1, 200000) AS g;

INSERT INTO app.audit_notes (customer_id, note, written_at)
SELECT 1 + (g % 10000), repeat('Long audit note text ' || g || '. ', 400), timestamptz '2025-04-01' + g * interval '1 hour'
FROM generate_series(1, 5000) AS g;

ANALYZE;
