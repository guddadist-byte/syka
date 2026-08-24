-- "Тихий режим": don't push notifications while none of a user's
-- subscribed points are open; queue them and flush as one digest once
-- any of the user's points opens (see tasks.py's quiet-hours flush loop).

CREATE TABLE quiet_hours_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  INTEGER REFERENCES users(telegram_id)
);
INSERT INTO quiet_hours_config (id) VALUES (1);

CREATE TABLE pending_notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id     INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    chat_id         TEXT NOT NULL,
    short_id        TEXT NOT NULL,
    client_name     TEXT,
    preview_text    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_pending_notifications_user ON pending_notifications(telegram_id);
