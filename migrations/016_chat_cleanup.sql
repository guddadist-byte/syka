-- Чистка базы от чатов, которые давно никому не нужны.
--
-- До этой миграции единственным retention-ом был _prune_messages_loop: раз в
-- час он удаляет сообщения старше MESSAGE_RETENTION_DAYS и намеренно оставляет
-- строку чата. Строки chats не удалялись никогда, и их накопилось 7097 — из
-- них 2843 вообще без единого сохранённого сообщения.
--
-- Зачем это вообще нужно. Не ради места на диске: 7097 строк весят пару
-- мегабайт. Ради старта. hydrate_cache_from_db() читает SELECT * FROM chats
-- без всяких границ и на каждый чат делает ещё один запрос за последними 50
-- сообщениями — около 14 000 последовательных запросов до того, как бот
-- начнёт обслуживать Telegram. Эта цена растёт вместе с историей за все годы
-- и не убывает никогда.
--
-- is_enabled по умолчанию 0 — в отличие от startup_notify_config, который
-- специально включён сразу. Разница принципиальная: удаление чата каскадом
-- уносит его сообщения (messages.chat_id ... ON DELETE CASCADE, и
-- PRAGMA foreign_keys=ON здесь действительно стоит), а таблица messages — это
-- долговечная защита от повторных уведомлений. Такое не включают молча: сперва
-- человек смотрит цифру «сколько удалится», и только потом нажимает.
--
-- retention_days = 90, а не 30, при том что сообщения живут 30 дней. Запас
-- нужен из-за короткого выхода в _process_chat: у чата, которого нет ни в
-- кэше, ни в базе, last_message_at пустой, он проваливается мимо короткого
-- выхода и покупает один throttled-запрос get_messages(). Это ровно та цена,
-- что стоила 705-780 секунд на 1000 чатов и чинилась тремя коммитами подряд.
-- Порядок сортировки списка чатов в Avito не задокументирован, поэтому
-- «старое всё равно не попадёт в первую тысячу» — предположение, а не факт, и
-- опираться на него нельзя. Запас по возрасту — то, чем мы за это платим.

CREATE TABLE chat_cleanup_config (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    is_enabled     INTEGER NOT NULL DEFAULT 0,
    retention_days INTEGER NOT NULL DEFAULT 90,
    last_run_at    TEXT,
    last_deleted   INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by     INTEGER REFERENCES users(telegram_id)
);

INSERT INTO chat_cleanup_config (id) VALUES (1);

-- Индексов не добавляем сознательно: idx_chats_last_message(last_message_at
-- DESC) уже есть с 001, а оба NOT EXISTS в условии чистки покрыты
-- idx_chat_notes_chat(chat_id, created_at) из 013 и
-- idx_scheduled_replies_chat(chat_id, status) из 012.
