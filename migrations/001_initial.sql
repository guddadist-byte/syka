-- Initial schema. See README/plan for design rationale.
--
-- Migration files are applied once, in order, by database.py's runner
-- (keyed off PRAGMA user_version). Never edit this file after it has been
-- applied anywhere — add a new numbered migration instead.

CREATE TABLE users (
    telegram_id         INTEGER PRIMARY KEY,
    username             TEXT,
    full_name            TEXT,
    last_name            TEXT,
    role                 TEXT NOT NULL DEFAULT 'employee'
                             CHECK (role IN ('employee', 'manager', 'admin', 'director')),
    status               TEXT NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'approved', 'blocked')),
    on_shift             INTEGER NOT NULL DEFAULT 0,
    responsible_point_id INTEGER REFERENCES points(id),
    blocked_bot          INTEGER NOT NULL DEFAULT 0,
    rating_points        INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at          TEXT,
    approved_by          INTEGER REFERENCES users(telegram_id),
    last_seen_at         TEXT,
    last_start_at        TEXT
);

CREATE TABLE points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    address         TEXT,
    working_hours   TEXT,
    name_is_custom  INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE point_coordinates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id    INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT 'avito_sync' CHECK (source IN ('avito_sync', 'manual')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_point_coordinates_point ON point_coordinates(point_id);

CREATE TABLE avito_accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id            INTEGER REFERENCES points(id),
    avito_user_id       INTEGER NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    client_id           TEXT NOT NULL,
    client_secret       TEXT NOT NULL,
    access_token        TEXT,
    token_expires_at    TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    last_poll_at        TEXT,
    last_poll_error     TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE avito_items (
    item_id       TEXT PRIMARY KEY,
    point_id      INTEGER REFERENCES points(id),
    lat           REAL,
    lon           REAL,
    resolved_by   TEXT CHECK (resolved_by IN ('coords', 'manual')),
    resolved_at   TEXT
);
CREATE INDEX idx_avito_items_point ON avito_items(point_id);

CREATE TABLE subscriptions (
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    point_id    INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, point_id)
);

CREATE TABLE templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id    INTEGER REFERENCES points(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'ai_prompt')),
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_by  INTEGER REFERENCES users(telegram_id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    is_active   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_templates_point ON templates(point_id);

CREATE TABLE ai_config (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    base_url            TEXT NOT NULL DEFAULT 'https://tooken.club/v1',
    model               TEXT NOT NULL DEFAULT 'gpt-5.6-sol',
    api_key             TEXT,
    extra_header_name   TEXT NOT NULL DEFAULT 'X-Tooken-Client',
    extra_header_value  TEXT NOT NULL DEFAULT 'codex',
    system_prompt       TEXT,
    is_enabled          INTEGER NOT NULL DEFAULT 1,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by          INTEGER REFERENCES users(telegram_id)
);
INSERT INTO ai_config (id) VALUES (1);

CREATE TABLE proxy_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled      INTEGER NOT NULL DEFAULT 0,
    proxy_url       TEXT,
    proxy_login     TEXT,
    proxy_password  TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      INTEGER REFERENCES users(telegram_id)
);
INSERT INTO proxy_config (id) VALUES (1);

CREATE TABLE payment_config (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled      INTEGER NOT NULL DEFAULT 0,
    amount_stars    INTEGER NOT NULL DEFAULT 100,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      INTEGER REFERENCES users(telegram_id)
);
INSERT INTO payment_config (id) VALUES (1);

CREATE TABLE payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(telegram_id),
    telegram_charge_id  TEXT NOT NULL,
    amount_stars        INTEGER NOT NULL,
    paid_at             TEXT NOT NULL DEFAULT (datetime('now')),
    refunded_at         TEXT
);
CREATE INDEX idx_payments_user ON payments(user_id);

CREATE TABLE welcome_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    text        TEXT NOT NULL DEFAULT 'Добро пожаловать! Ваша заявка одобрена.',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  INTEGER REFERENCES users(telegram_id)
);
INSERT INTO welcome_config (id) VALUES (1);

CREATE TABLE backup_config (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled              INTEGER NOT NULL DEFAULT 0,
    interval_hours          INTEGER NOT NULL DEFAULT 24,
    recipient_telegram_id   INTEGER,
    last_backup_at          TEXT,
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by              INTEGER REFERENCES users(telegram_id)
);
INSERT INTO backup_config (id) VALUES (1);

CREATE TABLE snoozes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id),
    remind_at   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    fired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_snoozes_due ON snoozes(fired, remind_at);

CREATE TABLE broadcasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id       INTEGER NOT NULL REFERENCES users(telegram_id),
    text            TEXT,
    photo_file_id   TEXT,
    sent_count      INTEGER NOT NULL DEFAULT 0,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE access_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id),
    action      TEXT NOT NULL CHECK (action IN
                    ('requested', 'approved', 'blocked', 'unblocked', 'role_changed')),
    actor_id    INTEGER REFERENCES users(telegram_id),
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_access_requests_user ON access_requests(user_id);

CREATE TABLE chats (
    chat_id             TEXT PRIMARY KEY,
    avito_account_id    INTEGER NOT NULL REFERENCES avito_accounts(id) ON DELETE CASCADE,
    point_id            INTEGER REFERENCES points(id),
    item_id             TEXT,
    client_name         TEXT,
    last_message_at     TEXT,
    last_message_text   TEXT,
    last_message_dir    TEXT CHECK (last_message_dir IN ('in', 'out')),
    unread_count        INTEGER NOT NULL DEFAULT 0,
    last_replied_by     INTEGER REFERENCES users(telegram_id),
    last_replied_at     TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_chats_point ON chats(point_id);
CREATE INDEX idx_chats_last_message ON chats(last_message_at DESC);

CREATE TABLE messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             TEXT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
    avito_message_id    TEXT,
    message_uuid        TEXT,
    direction           TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    text                TEXT,
    has_image           INTEGER NOT NULL DEFAULT 0,
    sent_at             TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_messages_chat ON messages(chat_id, sent_at);
