-- Run once in the Supabase SQL editor.

-- 1. Link ledger entries to a customer (this is what makes statements possible)
ALTER TABLE journal_entries
    ADD COLUMN IF NOT EXISTS customer_id INTEGER REFERENCES customers(customer_id);
CREATE INDEX IF NOT EXISTS idx_journal_entries_customer ON journal_entries(customer_id);

-- 2. One account code per customer
CREATE UNIQUE INDEX IF NOT EXISTS ux_customers_account_code ON customers(account_code);

-- 3. Accounts the POS needs. Skipped if a matching code OR name already exists.
--    Run SELECT * FROM chart_of_accounts first; if yours use other codes,
--    change ACCOUNTS in common.py instead of creating duplicates here.
INSERT INTO chart_of_accounts (account_code, account_name, account_type)
SELECT v.code, v.name, v.type
FROM (VALUES
    ('1100', 'Accounts Receivable',      'Asset',     '%receivable%'),
    ('2100', 'VAT Payable (Output VAT)', 'Liability', '%vat%'),
    ('4000', 'Sales Revenue',            'Revenue',   '%sales%'),
    ('5000', 'Cost of Goods Sold',       'Expense',   '%cost of goods%')
) AS v(code, name, type, pattern)
WHERE NOT EXISTS (
    SELECT 1 FROM chart_of_accounts c
    WHERE c.account_code = v.code OR c.account_name ILIKE v.pattern
);

-- 4. If journal_entries.reference_type has a CHECK constraint, it must allow
--    'SALE' and 'CUST_RECEIPT' (besides EXPENSE, STOCK_IN, RESTOCK).
--    Find it with:  SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--                   WHERE conrelid = 'journal_entries'::regclass;
