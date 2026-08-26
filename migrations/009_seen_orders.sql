-- Durable "already notified about this order" marker, mirroring
-- messages.avito_message_id dedup — without it, every active order would
-- look "new" again on every poll cycle (and after every restart).

CREATE TABLE seen_avito_orders (
    order_id            TEXT PRIMARY KEY,
    avito_account_id    INTEGER NOT NULL REFERENCES avito_accounts(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
