# Установка на хостинг

Бот — это обычный Python-процесс (aiogram 3 + SQLite), которому нужен постоянно
работающий Linux-сервер с shell-доступом (VPS/VDS). **Чистый FTP-хостинг без SSH
для этого не подходит** — по FTP можно только закачать файлы, но нельзя ни
поставить Python-зависимости, ни запустить и держать процесс живым (для этого
нужен systemd, а он управляется только через shell). Поэтому ниже:

- **Вариант A (SSH)** — полная установка, единственный по-настоящему рабочий способ.
- **Вариант B (FTP + SSH)** — если вам просто удобнее закачивать файлы через
  FileZilla/WinSCP, а не через `git clone`/`scp`, но команды установки (venv,
  systemd) всё равно выполняются по SSH — FTP тут только для передачи файлов.

Если хостинг вообще не даёт SSH (только FTP и панель управления) — сначала
уточните у хостера, есть ли способ запускать долгоживущие процессы (cron,
Node/Python "worker", Docker) через панель. Без этого бот работать не будет.

---

## 0. Что понадобится заранее

1. **Токен бота** — создать нового бота у [@BotFather](https://t.me/BotFather)
   командой `/newbot`, сохранить токен вида `123456789:ABC-DEF...`.
2. **Ваш Telegram id** — узнать у [@userinfobot](https://t.me/userinfobot)
   (нужен для `SUPERADMIN_TELEGRAM_ID` — первый «Админ» бутстрапится
   автоматически при первом запуске).
3. **VPS/VDS**: Debian или Ubuntu, минимум 1 vCPU / 512 МБ RAM, Python 3.12+.
   Всё остальное (Avito client_id/secret, ключ ИИ, прокси) можно ввести позже
   прямо в боте через «⚙️ Настройки» — на этом этапе не обязательно.

---

## Вариант A — установка по SSH (рекомендуется)

### 1. Подключение и базовые пакеты

```bash
ssh root@ваш_сервер

apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git
python3 --version   # должно быть 3.12+; если ниже — см. раздел «Python 3.12 на старом Debian/Ubuntu» ниже
```

### 2. Отдельный системный пользователь (без root)

```bash
useradd --system --create-home --shell /usr/sbin/nologin avitobot
mkdir -p /opt/avito_bot /var/log/avito_bot
chown -R avitobot:avitobot /opt/avito_bot /var/log/avito_bot
```

### 3. Код проекта

```bash
su - avitobot -s /bin/bash
cd /opt/avito_bot
git clone https://github.com/guddadist-byte/syka.git .
```

Если у сервера нет доступа к GitHub напрямую (закрытая сеть) — см. Вариант B,
залейте файлы по FTP вместо `git clone`.

### 4. Виртуальное окружение и зависимости

```bash
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

### 5. Конфигурация — `.env`

```bash
cp .env.example .env
nano .env
```

Заполните минимум:

```ini
BOT_TOKEN=123456789:ABC-DEF...        # из @BotFather
DB_PATH=/opt/avito_bot/avito_bot.db
SUPERADMIN_TELEGRAM_ID=123456789      # ваш id из @userinfobot
LOG_LEVEL=INFO
PID_FILE=/opt/avito_bot/avito_bot.pid
CREDENTIALS_PATH=/opt/avito_bot/credentials.toml
```

`DB_PATH`/`PID_FILE`/`CREDENTIALS_PATH` можно оставить как в примере, если
рабочая директория действительно `/opt/avito_bot`.

### 6. (Необязательно) `credentials.toml` — сразу вписать Avito/ИИ ключи

Если под рукой уже есть `client_id`/`client_secret` Avito-аккаунтов и ключ
tooken.club — можно один раз вписать их в файл вместо форм в боте:

```bash
cp credentials.example.toml credentials.toml
nano credentials.toml
```

Это необязательно — всё то же самое можно ввести позже через
«⚙️ Настройки» в самом боте. Файл читается один раз при старте и не трогает
значения, которые вы уже поменяли через бота.

### 7. systemd-юнит

```bash
exit   # обратно в root/sudo-пользователя
cp /opt/avito_bot/avito_bot.service /etc/systemd/system/avito_bot.service
systemctl daemon-reload
systemctl enable avito_bot
systemctl start avito_bot
```

### 8. Проверка

```bash
systemctl status avito_bot
tail -f /var/log/avito_bot/bot.log /var/log/avito_bot/bot.err.log
```

Затем откройте бота в Telegram и отправьте `/start` — если вы указали свой id
в `SUPERADMIN_TELEGRAM_ID`, вы сразу должны попасть в главное меню с ролью
«👑 Админ». Зайдите в «⚙️ Настройки» и добавьте Avito-аккаунты, ключ ИИ и,
при необходимости, прокси (если Telegram заблокирован в регионе сервера).

### Python 3.12 на старом Debian/Ubuntu

Если `apt install python3` даёт версию ниже 3.12 (например, Ubuntu 20.04/22.04):

```bash
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv
# дальше вместо `python3 -m venv venv` используйте:
python3.12 -m venv venv
```

---

## Вариант B — файлы по FTP, установка по SSH

Используйте, если вам удобнее закачивать файлы через FileZilla/WinSCP, чем
через `git clone` (например, сервер без доступа к GitHub, или вы правите
файлы локально и хотите просто перезалить их). Команды установки (шаги 4-8
из варианта A) всё равно выполняются по SSH — FTP тут отвечает только за то,
чтобы файлы проекта оказались на сервере.

### 1. Подготовить файлы локально

На своём компьютере скачайте архив репозитория (на GitHub: `Code → Download
ZIP`) и распакуйте, либо просто используйте локальную папку проекта.

### 2. Создать пользователя и папки — как в шагах 1-2 варианта A (по SSH)

### 3. Залить файлы по FTP/SFTP

Подключитесь клиентом (FileZilla, WinSCP, Cyberduck):

- Хост: адрес вашего сервера
- Протокол: **предпочтительно SFTP** (порт 22, тот же логин/пароль или ключ,
  что и для SSH) — обычный FTP (порт 21) передаёт логин/пароль в открытом
  виде, использовать только если хостер не даёт SFTP
- Путь на сервере: `/opt/avito_bot`

Перетащите **всё содержимое** папки проекта (`main.py`, `handlers.py`,
`requirements.txt`, `migrations/`, `.env.example`, `credentials.example.toml`,
`avito_bot.service` и т.д.) в `/opt/avito_bot` на сервере.

Важно: FTP-клиенты часто не показывают скрытые файлы (начинающиеся с точки)
— `.env`/`.gitignore` создавайте/редактируйте потом прямо на сервере по SSH
(шаг 5 варианта A), а не через FTP.

### 4. Права доступа

По SSH (файлы, залитые по FTP, обычно принадлежат тому пользователю, под
которым был FTP-логин, — нужно передать их `avitobot`):

```bash
chown -R avitobot:avitobot /opt/avito_bot
```

### 5. Дальше — шаги 4-8 варианта A

Виртуальное окружение, `.env`, `credentials.toml`, systemd-юнит, запуск —
всё как в варианте A, только без `git clone` (файлы уже на месте).

---

## Обновление бота в будущем

**Если ставили через `git clone` (вариант A):**

```bash
su - avitobot -s /bin/bash
cd /opt/avito_bot
git pull
venv/bin/pip install -r requirements.txt   # если requirements.txt менялся
exit
systemctl restart avito_bot
```

**Если заливали по FTP (вариант B):** перезалейте изменившиеся файлы по
SFTP/FTP поверх старых, затем:

```bash
chown -R avitobot:avitobot /opt/avito_bot
systemctl restart avito_bot
```

Схема БД обновляется миграциями автоматически при следующем старте — ничего
руками накатывать не нужно.

---

## Частые проблемы

- **`systemctl status` показывает `failed` сразу после старта** — почти
  всегда это `.env`: проверьте `BOT_TOKEN`/`SUPERADMIN_TELEGRAM_ID` заполнены
  и без опечаток, смотрите `journalctl -u avito_bot -n 50` для точной ошибки.
- **`Another instance is already running (lock: ...)`** — где-то уже запущен
  второй процесс с тем же `.env`/`PID_FILE` (например, кто-то руками запустил
  `python main.py` поверх systemd-инстанса). Это защита от дублей уведомлений
  — остановите второй процесс, а не удаляйте pid-файл руками.
- **Бот не отвечает, но процесс жив** — проверьте, не заблокирован ли
  Telegram в регионе сервера; если да, включите прокси в «⚙️ Настройки»
  (после сохранения бот сам перезапустится за счёт `Restart=always`).
- **Permission denied при `git pull`/старте** — файлы принадлежат не тому
  пользователю: `chown -R avitobot:avitobot /opt/avito_bot`.

---

## Staging (тестовый) инстанс — по желанию

Для проверки ролей/рассылок/платного входа без риска для боевого бота —
второй, полностью независимый инстанс: отдельная папка
(например `/opt/avito_bot-staging`), свой `.env` со **вторым** ботом от
@BotFather, своя `DB_PATH`/`PID_FILE`, свой systemd-юнит
(`avito_bot-staging.service`, скопированный с другим `WorkingDirectory`/
`EnvironmentFile`). Повторите шаги варианта A целиком во второй папке.
