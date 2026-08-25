-- Persisted "read up to here" boundary for a chat, independent of the
-- transient unread_count. Without this, "✅ Прочитано" (marking a chat
-- read without actually replying) only zeroed an in-memory counter that
-- got silently recomputed from raw message history (and lost) on the
-- next new message or process restart — see tasks.py/bot_cache.py.

ALTER TABLE chats ADD COLUMN read_at TEXT;
