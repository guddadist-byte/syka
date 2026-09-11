-- "Отправить позже": a reply written now and delivered to the Avito client
-- at a chosen moment. Persisted rather than held in memory because a deploy
-- almost certainly lands between scheduling and sending, and a scheduled
-- reply has to survive it.
--
-- message_uuid is generated once, when the row is created, so a retry would
-- carry the same X-Idempotency-Key. That is a belt-and-braces measure only:
-- Avito's support for the header is unconfirmed (see the plan's known
-- limitations), so the real guard against a double send is the atomic
-- pending -> sending claim in database.claim_scheduled_reply().
--
-- 'sending' is a terminal state in practice: a row stuck there means the
-- process died between the claim and the send. It is never retried — for a
-- message to a customer, "not sent" beats "sent twice" — and it stays
-- visible in the chat card so nothing is lost silently.

CREATE TABLE scheduled_replies (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          TEXT NOT NULL,
    avito_account_id INTEGER NOT NULL,
    author_id        INTEGER REFERENCES users(telegram_id),
    text             TEXT NOT NULL,
    message_uuid     TEXT NOT NULL,
    send_at          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'sending', 'sent', 'canceled', 'failed')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at          TEXT,
    error            TEXT
);

CREATE INDEX idx_scheduled_replies_due ON scheduled_replies(status, send_at);
CREATE INDEX idx_scheduled_replies_chat ON scheduled_replies(chat_id, status);
