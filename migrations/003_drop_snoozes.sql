-- The "snooze" (⏰ Через 2 часа / 🌅 Завтра утром) feature was removed —
-- the chat-detail screen no longer offers it, so the table is dead.

DROP INDEX IF EXISTS idx_snoozes_due;
DROP TABLE IF EXISTS snoozes;
