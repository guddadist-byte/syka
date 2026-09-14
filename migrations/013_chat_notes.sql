-- Internal notes on a chat: "торгуется, минимум 15000", "обещал подъехать
-- в среду". Staff-only — a note is never sent anywhere near Avito, it is
-- read and written exclusively by our own screens.
--
-- Notes outlive a shift, which is the point: what one person learns on the
-- phone today is what the next person needs tomorrow.
--
-- No FK on chat_id, matching scheduled_replies: the chats row is always
-- there in practice (the poller creates it long before anyone can open the
-- chat), and losing a note to a constraint would be worse than an orphan.

CREATE TABLE chat_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,
    author_id  INTEGER REFERENCES users(telegram_id),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_chat_notes_chat ON chat_notes(chat_id, created_at);
