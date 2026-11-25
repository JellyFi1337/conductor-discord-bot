# Conductor в Docker

Стек — **postgres** и **bot**. `BOT_MODE` в `.env` выбирает, какой бот поднимется в `bot`. Чтобы запустить второго одновременно, нужен отдельный файл `.env.test` и контейнер `bot-test` (профиль `test`). Ни очередей, ни Redis, ни входящего HTTP-порта боту не нужно: он сам ходит наружу к Discord. Postgres слушает только `127.0.0.1`.

Матрица переменных окружения — одна на весь проект, она в [README](../README.md#переменные-окружения). Здесь только то, что относится к контейнерам.

---

## 1. Состав стека

| Сервис | Зачем | Как включается |
| --- | --- | --- |
| `bot` | основной Conductor | всегда |
| `bot-test` | второй процесс из `.env.test` | `docker compose --profile test up -d` |
| `postgres` | база (общая, строки разделены по `guild_id`) | всегда |
| `adminer` | просмотр таблиц на `127.0.0.1:8080` | `docker compose --profile tools up -d` |

### Образ

Python 3.12 slim, пакет `conductor[postgres]` (драйвер `asyncpg`), процесс от пользователя `conductor` (uid 1000). В образе жёстко задан `DATA_DIR=/app/data`: пакет установлен в `site-packages`, поэтому «каталог рядом с проектом» внутри контейнера смысла не имеет.

Эталоны конфигов лежат в образе отдельно от каталога данных — в `/app/defaults/`. Том хоста их не перекрывает.

### Запуск контейнера

`docker/entrypoint.sh` делает четыре вещи и уходит:

1. создаёт `DATA_DIR`, кладёт туда `automod.yml` из эталона, **если файла ещё нет** (существующий не трогает), плюс копии обоих `*.yml.example` для правки на месте;
2. отдаёт каталог пользователю `conductor`;
3. собирает `DATABASE_URL=postgresql+asyncpg://…@postgres:5432/…` из `POSTGRES_*` с экранированием пароля — значение `DATABASE_URL` из `.env` при этом игнорируется;
4. включает `AUTO_MIGRATE=true`, если ключ пуст.

### Тома

| Путь | Что там | Тип |
| --- | --- | --- |
| `postgres_data` | база | именованный том Docker |
| `./data` → `/app/data` у `bot` | `automod.yml`, `.command-hash`, `.heartbeat` | **каталог репозитория** |
| `./data-test` → `/app/data` у `bot-test` | то же для тестового процесса | **каталог репозитория** |

Каталог данных примонтирован с хоста намеренно: правила автомода правятся обычным редактором, а не `docker compose cp`. После правки — кнопка **«Перечитать файл»** на карточке в `#автомод`, рестарт не нужен.

На сервере с уже существующим `data/automod.yml` новые правила (`everyone_mention`, более строгие исключения у `invite_link`) включатся из встроенных умолчаний бота: ключ, которого нет в файле, подставляется сам. Чтобы изменить лимит, тайм-аут или исключения — допишите блок правила. Выключить правило: `action: off`.

Файлы в `./data` принадлежат uid 1000 (пользователь контейнера). Если ваш логин на сервере с другим uid, для правки понадобится `sudo`.

> Обновление со старой схемы: раньше `/app/data` жил в безымянном томе `bot_data`. Переносить оттуда нечего — `.command-hash` пересоберётся сам, `automod.yml` положит entrypoint. Забрать старый файл, если правили его в контейнере: `docker compose cp bot:/app/data/automod.yml ./data/automod.yml` **до** пересборки, затем `docker volume rm conductor_bot_data`.

### Healthcheck

У бота нет порта, поэтому живость показывает файл: планировщик раз в 30 секунд трогает `.heartbeat`, healthcheck считает контейнер больным, если файлу больше 120 секунд. Это ловит зависший цикл событий — то, чего не видно по «процесс запущен». Первые две минуты после старта (`start_period`) проверка не применяется: бот в это время делает перепись и восстановление панелей.

```bash
docker compose ps          # колонка STATUS: healthy / unhealthy
```

`restart: unless-stopped` поднимает контейнеры после ребута. Остановка бота — SIGTERM и 30 секунд на дренаж очереди логов.

### Сеть и DNS

Контейнеры используют DNS хоста через встроенный DNS Docker (`127.0.0.11`). Внешние DNS в `docker-compose.yml` намеренно не заданы: в сетях, где `1.1.1.1` и `8.8.8.8` недоступны, такая настройка полностью отрезает бота от Discord.

Если на хосте DNS нестабилен (в логе часто `Timeout while contacting DNS servers`), сначала проверьте DNS самого хоста (`resolvectl status`). Если нужен свой DNS для бота, задайте его в `docker-compose.override.yml` рядом с основным файлом — он не отслеживается git и переживает `git pull`:

```yaml
services:
  bot:
    dns: ["<рабочий DNS вашей сети>"]
```

Перед этим убедитесь, что адрес отвечает из контейнера: `docker run --rm --dns <адрес> conductor:local python -c "import socket; print(socket.getaddrinfo('discord.com', 443)[0][4])"`.

Таймауты `aiodns` с последующим `RESUMED` — нормальное переподключение шлюза Discord, данные не теряются. Счётчик переподключений за час — вкладка «Сейчас» в `/diagnose`. Логгеры `discord.client` и `discord.gateway` не глушить: по ним видно, что шлюз жив.

Проверка на хосте и в контейнере:

```bash
resolvectl status
docker compose exec bot getent hosts discord.com
docker compose exec bot getent hosts postgres
```

`getent hosts postgres` должен показать адрес контейнера базы в сети Compose.

---

## 2. Что нужно заранее

1. Два приложения бота в [Discord Developer Portal](https://discord.com/developers/applications): основное и тестовое. Пошагово — [SETUP.md](SETUP.md).
2. Privileged Gateway Intents у обоих: **Server Members**, **Message Content**. **Public Bot** выключен.
3. Права бота при приглашении — список `REQUIRED_PERMISSIONS` (см. [SETUP.md](SETUP.md), «Права и роль бота»).
4. Id серверов, на которых этому процессу можно быть.
5. Файл `.env` в корне репозитория, скопированный с `.env.example`.

Обязательный минимум в `.env`:

```
BOT_MODE=live
DISCORD_TOKEN=токен-основного-приложения
GUILD_IDS=id-основного-сервера
TEST_DISCORD_TOKEN=токен-тестового-приложения
TEST_GUILD_IDS=id-тестового-сервера
POSTGRES_PASSWORD=длинный-случайный-пароль
```

Чтобы переключить этот же контейнер на тестового, поставьте `BOT_MODE=test`. Токен и серверы тестового бота уже лежат в том же файле.

Оба сразу: скопируйте `.env` в `.env.test`, там `BOT_MODE=test` и токен тестового приложения, затем `docker compose --profile test up -d`.

`RATE_PROFILE` (умолчание `edge`) и переопределения `GLOBAL_RPS_TARGET`, `OUTBOUND_CONCURRENCY`, `MAX_RATELIMIT_WAIT`, `INVALID_REQUEST_STOP` задают темп REST. `INTERACTION_LIMIT_PER_MINUTE` — потолок нажатий на гильдию; `50` значит взять значение профиля. Два бота на одном сервере делят бюджет недопустимых запросов Discord (10 000 за 10 минут на IP). При сбоях поставьте `RATE_PROFILE=safe` и перезапустите.

Запасные ключи `PUBLIC_GUILD_ID` / `TEST_GUILD_ID` читаются, только если список выбранного режима пуст.

Пароль лучше из латиницы и цифр. Символы `@ : / # ? %` ломают URL, если задавать `DATABASE_URL` руками; Compose собирает URL сам и экранирует. Без `POSTGRES_PASSWORD` `docker compose up` останавливается сразу.

---

## 3. Windows 11

### Docker Desktop

1. Поставьте [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/), в установщике оставьте **Use WSL 2 instead of Hyper-V**.
2. Запустите его и дождитесь зелёного статуса в трее.
3. Проверьте: `docker version` и `docker compose version` печатают версии, а не ошибку.

### Куда положить репозиторий

Сборка из OneDrive или из пути с кириллицей ломается чаще, чем работает. Надёжнее клон вне OneDrive:

```powershell
mkdir C:\src -ErrorAction SilentlyContinue
cd C:\src
git clone <url-репозитория> Conductor
cd C:\src\Conductor
```

### `.env` и первый запуск

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Id сервера: включите в Discord Режим разработчика → ПКМ по серверу → Копировать ID. Сохраните файл в UTF-8.

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\start.ps1
```

`start.ps1` вызывает `docker-up.ps1` (проверка Docker и `.env`, остановка старых копий, `docker compose up -d --build`) и показывает логи. Двойной щелчок по `start.bat` — то же самое. Без логов — `.\docker-up.ps1`. Те же команды вручную: `docker compose up -d --build`.

Первая сборка качает Python и Postgres — несколько минут. Дальше старт занимает секунды.

### Проверить, что бот жив

```powershell
docker compose ps
docker compose logs -f bot
```

В логе — строка `preflight` с двумя guild id и подключение к Discord. `Ctrl+C` закрывает только просмотр логов.

### Повседневные команды

| Действие | Команда |
| --- | --- |
| Статус и здоровье | `docker compose ps` |
| Логи бота / базы | `docker compose logs -f bot` · `docker compose logs -f postgres` |
| Перезапуск бота | `docker compose restart bot` |
| Остановить все копии (локальный Python и контейнеры) | `.\stop.ps1` |
| Остановить, контейнеры оставить | `docker compose stop` |
| Убрать контейнеры, базу сохранить | `docker compose down` |
| Убрать вместе с базой | `docker compose down -v` — **сотрёт данные** |
| С чистого листа (файлы + схема Postgres) | `.\stop.ps1`, затем `python scripts/wipe_data.py --yes` |
| Ревизия схемы | `docker compose exec bot conductor db current` |
| Миграции вручную | `docker compose exec bot conductor db upgrade` |
| Консоль Postgres | `docker compose exec postgres psql -U conductor -d conductor` |
| Таблицы в браузере | `docker compose --profile tools up -d`, затем http://127.0.0.1:8080 |

В Adminer: System = PostgreSQL, Server = `postgres`, остальное — как в `.env`.

Снимок базы:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T postgres pg_dump -U conductor -d conductor --clean --if-exists | Set-Content -Encoding Byte backups\conductor.sql
```

### Обновление

```powershell
git pull
.\start.ps1
```

Скрипт гасит старые копии, пересобирает образ, поднимает стек. Том Postgres не трогается.

---

## 4. Ubuntu 24.04

Полная инструкция с пустого сервера — Docker, firewall, `.env`, systemd, бэкапы и чеклист: **[deploy/ubuntu-24.04/README.md](../deploy/ubuntu-24.04/README.md)**.

Кратко:

```bash
sudo git clone <url-репозитория> /opt/conductor
sudo chown -R "$USER:$USER" /opt/conductor
cd /opt/conductor
sudo bash deploy/ubuntu-24.04/bootstrap.sh --ufw
nano .env
./deploy/ubuntu-24.04/check-env.sh
./deploy/ubuntu-24.04/start.sh
```

После bootstrap **перелогиньтесь по SSH** (группа `docker`). Старые пути `deploy/ubuntu/setup.sh` и `deploy/backup-postgres.sh` вызывают тот же пакет.

---

## 5. Миграции

В Compose `AUTO_MIGRATE=true`, схема догоняется при старте. Если поднимаете Postgres иначе — задайте `AUTO_MIGRATE=true` **или** выполните `conductor db upgrade` до старта: с выключенными миграциями бот отказывается стартовать на схеме старше head. `/diagnose` → «Настройка» показывает строку «схема БД = head миграций».

Текущий head — **0073** (`bug_reports`). Обновление с 0.8.1 / схемы 0071 поднимает две ревизии подряд:

| Ревизия | Что добавляет |
| --- | --- |
| `0072_groups_archive` | архив групп, приглашения, порог репутации и квоты |
| `0073_bug_reports` | таблица отчётов `/bug` |

После `git pull` и `.\start.ps1` (или `docker compose up -d --build bot`) entrypoint прогонит Alembic сам. Проверка: в логе старта `startup_ready` с `version=0.9.0` и `alembic=0073`.

Новые правила автомода (`everyone_mention`, строже исключения у `invite_link`) подставляются из встроенных умолчаний, если ключа нет в `data/automod.yml`. Чтобы поменять лимит — допишите блок; выключить правило: `action: off`. Кнопка «Перечитать файл» в `#автомод`.

DNS контейнеров — DNS хоста (см. «Сеть и DNS»). Имя сервиса базы — `postgres`.

Отчёты об ошибках: канал `#баг-репорты` в категории следящих (строка каркаса `bug_reports`). Если канала нет — `/diagnose` → Каркас → починить или Собрать сервер. Кнопки «Принято» / «Исправлено» / «Не баг» видят senior, chief и special.

Внешний управляемый Postgres вместо контейнера: задайте полный `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname` и не задавайте `POSTGRES_HOST` — иначе entrypoint перепишет URL на контейнер в сети Compose.

Старый локальный SQLite (`data/conductor.db`) и Postgres в Docker — **разные базы**. Автоматического переноса нет.

---

## 6. Частые ошибки

| Симптом | Что проверить |
| --- | --- |
| `docker` не найден / daemon not running | Docker Desktop запущен (Windows) или `sudo systemctl start docker` (Ubuntu) |
| `Set POSTGRES_PASSWORD in .env` | ключа нет в `.env` |
| `В .env нет DISCORD_TOKEN` | пусто или остался плейсхолдер `your-bot-token` |
| `нужен список серверов этого бота` | задайте `GUILD_IDS` или запасные `PUBLIC_GUILD_ID` / `TEST_GUILD_ID` |
| Контейнер `unhealthy`, в логах тихо | `.heartbeat` не обновляется: смотрите `docker compose logs bot` — скорее всего процесс не дошёл до планировщика |
| Сборка падает на Windows, странные пути | клонируйте в `C:\src\Conductor`, не в OneDrive |
| `$'\r': command not found` в entrypoint | `.sh` должны быть с LF; в репозитории это задано `.gitattributes` |
| Бот сразу выходит с сервера | так и задумано: забор гильдий |
| Слэш-команд нет на нужном сервере | этот id не в `GUILD_IDS` этого процесса; основной и тестовый — разные токены |
| Команды не обновились после `git pull` | хеш дерева не изменился — `FORCE_SYNC=true` на один запуск |
| `/diagnose` пишет «файл правил не найден» | нет `data/automod.yml`; entrypoint кладёт его только когда каталог пуст |
| Бот не в сети, в логах timeout | исходящий 443, DNS (`getent hosts discord.com` в контейнере); `RESUMED` после таймаута — нормальное переподключение |
| `permission denied` на `.env` | `chmod 600 .env`, владелец — тот, кто запускает compose |
| После ребута Ubuntu бота нет | `sudo systemctl enable --now docker conductor` |

Разбор прав и интентов на конкретном сервере — команда `/diagnose`.
