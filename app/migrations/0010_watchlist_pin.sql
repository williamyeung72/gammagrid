-- Pin is a mark on the watchlist row. A ticker that is not on the list
-- cannot be pinned, and a contract pin stays on tracked_contracts.
-- DEFAULT false covers rows already stored. import_sqlite inserts ticker
-- only, and this default fills the new column.
ALTER TABLE watchlist
    ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT false;
