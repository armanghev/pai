-- Name and type (equity, etf, mutual fund, cryptocurrency, cash...) for each position, so
-- individual holdings can be shown, not just account totals.
ALTER TABLE holdings_snapshots ADD COLUMN name TEXT;
ALTER TABLE holdings_snapshots ADD COLUMN security_type TEXT;
