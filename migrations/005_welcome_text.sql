-- Fuller default welcome message describing employee-facing capabilities,
-- shown right after an access request is approved. Only applied if nobody
-- has customized it via the admin panel yet (updated_by IS NULL) — same
-- "seed only if untouched" rule used elsewhere (see
-- database.seed_from_credentials_file).

UPDATE welcome_config
SET text = '👋 Добро пожаловать в CRM-бот «Гудда»!

Здесь вы ведёте переписку с клиентами Avito прямо из Telegram, без захода в приложение Авито.

Что умеет бот:
📩 <b>Непрочитанные</b> — чаты, где клиент ждёт ответа
🕒 <b>Недавние</b> — то, что вы уже ответили за последний час
💬 Ответ клиенту — просто напишите текст или пришлите фото прямо в чате, они уйдут в Avito
🧠 <b>ИИ-ответ</b> — черновик ответа на вопрос клиента (без цен и оценки товара — это только вручную)
📋 <b>Шаблоны</b> — готовые ответы на частые вопросы вашей точки
📍 <b>Мои точки</b> — подпишитесь на свою точку, чтобы получать уведомления именно по ней
💼 Смена — включайте, когда готовы отвечать клиентам: уведомления приходят только на смене
👤 <b>Мой профиль</b> — ваш рейтинг и место в общем зачёте

Чтобы начать: нажмите «💼 На смене», затем «📍 Мои точки» и подпишитесь на нужный адрес — после этого начнут приходить уведомления о новых сообщениях.',
    updated_at = datetime('now')
WHERE id = 1 AND updated_by IS NULL;
