-- "Бот запущен" — a status report to the owners on every start.
--
-- is_enabled defaults to 1, unlike every other singleton config here. The
-- others are opt-in extras; this one was asked for directly, and shipping
-- it off would mean the requested behaviour does not happen until somebody
-- goes hunting for a switch.
--
-- last_heartbeat_at is refreshed once a minute by tasks._heartbeat_loop, so
-- a start can report how long the bot was actually down. last_stopped_at is
-- written during graceful shutdown, which is what separates "I deployed" от
-- "it died at 3am and nobody noticed": if the stop mark is older than the
-- last heartbeat, the process never got to shut down cleanly.

CREATE TABLE startup_notify_config (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled            INTEGER NOT NULL DEFAULT 1,
    recipient_telegram_id INTEGER,
    last_heartbeat_at     TEXT,
    last_stopped_at       TEXT,
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by            INTEGER REFERENCES users(telegram_id)
);

INSERT INTO startup_notify_config (id) VALUES (1);
