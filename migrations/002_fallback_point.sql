-- Adds a synthetic "fallback" point for chats with no ad-level geo data at
-- all (direct-to-profile messages, or ads whose location Avito didn't
-- return) — see database.get_or_create_fallback_point().
--
-- SQLite can't alter a CHECK constraint in place, so avito_items is
-- recreated with the widened constraint and its data copied over.

ALTER TABLE points ADD COLUMN is_fallback INTEGER NOT NULL DEFAULT 0;

CREATE TABLE avito_items_new (
    item_id       TEXT PRIMARY KEY,
    point_id      INTEGER REFERENCES points(id),
    lat           REAL,
    lon           REAL,
    resolved_by   TEXT CHECK (resolved_by IN ('coords', 'manual', 'fallback')),
    resolved_at   TEXT
);
INSERT INTO avito_items_new SELECT * FROM avito_items;
DROP TABLE avito_items;
ALTER TABLE avito_items_new RENAME TO avito_items;
CREATE INDEX idx_avito_items_point ON avito_items(point_id);
