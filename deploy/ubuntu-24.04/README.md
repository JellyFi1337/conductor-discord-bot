# Conductor на пустом Ubuntu 24.04

Инструкция для связки **Windows (разработка)** → **приватный GitHub** → **Ubuntu 24.04 (сервер)**. С Windows вы заходите по SSH, код обновляете через `git pull`, готовый `.env` перетаскиваете в FileZilla. Приложения Discord и `/owner seed` — в [docs/SETUP.md](../../docs/SETUP.md). Здесь только машина.

Скрипты рассчитаны на **Ubuntu 24.04 LTS** и официальный **Docker Engine** (не snap). Каталог на сервере — `/opt/conductor`.

---

## Содержание

0. [Как это устроено](#0-как-это-устроено)
1. [SSH с Windows](#1-ssh-с-windows)
2. [Первый вход на сервер](#2-первый-вход-на-сервер)
3. [Приватный репозиторий](#3-приватный-репозиторий)
4. [Bootstrap: Docker, systemd, firewall](#4-bootstrap-docker-systemd-firewall)
5. [FileZilla и готовый `.env`](#5-filezilla-и-готовый-env)
6. [Права папок, файлов и Docker](#6-права-папок-файлов-и-docker)
7. [Проверка `.env` и первый запуск](#7-проверка-env-и-первый-запуск)
8. [Проверка в Discord](#8-проверка-в-discord)
9. [Повседневные команды](#9-повседневные-команды)
10. [Как выкатывать правки с Windows](#10-как-выкатывать-правки-с-windows)
11. [Резервные копии](#11-резервные-копии)
12. [После ребута](#12-после-ребута)
13. [Второй бот одновременно](#13-второй-бот-одновременно)
14. [Чеклист](#14-чеклист)
15. [Частые ошибки](#15-частые-ошибки)
16. [Состав пакета](#16-состав-пакета)

---

## 0. Как это устроено

```
Windows                         GitHub (private)                 Ubuntu
───────                         ────────────────                 ──────
код, коммит, push  ──────────►  репозиторий        ──────────►  git pull / update.sh
PowerShell: ssh conductor  ─────────────────────────────────►  консоль сервера
FileZilla (SFTP)           ─────────────────────────────────►  только .env и дампы
```

| Что | Чем возить | Чего не делать |
| --- | --- | --- |
| Код бота | git push на Windows, `update.sh` на сервере | не заливать весь проект FileZilla вместо git |
| `.env`, `.env.test` | FileZilla (SFTP) или `scp` | не коммитить в git |
| Дампы базы | FileZilla / `scp` из `backups/` | не открывать Postgres в интернет |
| `automod.yml` | править на сервере или перетащить FileZilla | не класть в публичный чат |

На сервере крутятся контейнеры **postgres** и **bot**. Бот сам ходит к Discord, входящий HTTP не нужен. Postgres слушает только `127.0.0.1`. После ребута стек поднимается сам.

---

## 1. SSH с Windows

Цель: в PowerShell или Windows Terminal писать `ssh conductor` и сразу попадать в Ubuntu **по ключу**, без пароля каждый раз. Тот же ключ потом подключите в FileZilla.

### 1.1. Что взять у хостера

В панели VPS (Timeweb, Selectel, Hetzner, DigitalOcean и т.п.):

1. Создайте сервер: образ **Ubuntu 24.04 LTS**, публичный **IPv4**.
2. Запишите **IP**, пользователя (часто `root` или `ubuntu`) и пароль из письма / панели. Пароль нужен один раз.
3. В **облачном firewall** откройте **22/tcp** с вашего домашнего IP, если умеете ограничить. 5432 и 8080 не открывайте.
4. 1 ГБ RAM минимум, лучше 2 ГБ. 10 ГБ диска хватает.

Если SSH сразу не пускает — откройте **VNC / serial console** в панели хостера и проверьте, что машина загрузилась.

### 1.2. Клиент SSH на Windows

В Windows 10/11 клиент обычно уже есть. Проверьте в PowerShell:

```powershell
ssh -V
```

Если команды нет:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

Или: Параметры → Приложения → Дополнительные компоненты → OpenSSH Client.

Удобнее работать из [Windows Terminal](https://aka.ms/terminal).

### 1.3. Ключ на Windows

В PowerShell:

```powershell
New-Item -ItemType Directory -Force $env:USERPROFILE\.ssh | Out-Null
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\conductor -C "conductor-windows"
```

Пароль на ключ (passphrase) — по желанию: тогда Windows спросит его при первом `ssh` в сессии. Пустой Enter — без пароля на ключ.

Появятся два файла:

| Файл | Куда |
| --- | --- |
| `C:\Users\<вы>\.ssh\conductor` | **приватный**. Никому не отправлять, на сервер не копировать |
| `C:\Users\<вы>\.ssh\conductor.pub` | **публичный**. Его кладут на Ubuntu |

Посмотреть публичный:

```powershell
Get-Content $env:USERPROFILE\.ssh\conductor.pub
```

### 1.4. Первый вход по паролю и установка ключа

Подставьте IP и пользователя из панели:

```powershell
ssh ubuntu@203.0.113.10
```

Если пускает только `root`:

```powershell
ssh root@203.0.113.10
```

В первый раз согласитесь на fingerprint (`yes`). Введите пароль из панели.

**Из нового окна PowerShell** (пока первый SSH ещё можно открыть паролем) отправьте публичный ключ. Одна строка:

```powershell
type $env:USERPROFILE\.ssh\conductor.pub | ssh ubuntu@203.0.113.10 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Если заходили как `root`, замените `ubuntu@` на `root@`.

Проверка — должен пустить **без пароля сервера**:

```powershell
ssh -i $env:USERPROFILE\.ssh\conductor ubuntu@203.0.113.10
```

### 1.5. Короткое имя `ssh conductor`

На Windows создайте или допишите файл `C:\Users\<вы>\.ssh\config` (пример — [examples/windows-ssh-config](examples/windows-ssh-config)):

```
Host conductor
  HostName 203.0.113.10
  User ubuntu
  IdentityFile ~/.ssh/conductor
  IdentitiesOnly yes
```

`HostName` и `User` замените на свои. Дальше:

```powershell
ssh conductor
```

### 1.6. Обычный пользователь, если пускает только root

Работать постоянно под root не нужно. На сервере:

```bash
adduser --gecos "" conductor
usermod -aG sudo conductor
mkdir -p /home/conductor/.ssh
cp /root/.ssh/authorized_keys /home/conductor/.ssh/authorized_keys
chown -R conductor:conductor /home/conductor/.ssh
chmod 700 /home/conductor/.ssh
chmod 600 /home/conductor/.ssh/authorized_keys
```

В Windows `config` поставьте `User conductor`. Проверьте `ssh conductor`. Потом в панели хостера можно отключить вход по паролю — **только когда вход по ключу уже работает**.

На сервере, когда ключ проверен:

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
```

Не закрывайте текущую сессию, пока из **второго** окна Windows не откроется новый `ssh conductor`. Иначе можно запереть себя снаружи.

---

## 2. Первый вход на сервер

Дальше команды на Ubuntu, если не подписано «на Windows». Пользователь — с `sudo`. `sudo` только где написано.

```bash
sudo hostnamectl set-hostname conductor
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y
sudo apt-get install -y ca-certificates curl git openssh-server
sudo systemctl enable --now ssh
```

Если ядро обновилось:

```bash
sudo reboot
```

С Windows зайдите снова: `ssh conductor`.

Не ставьте Docker из snap. Если уже стоит:

```bash
sudo snap remove docker
```

---

## 3. Приватный репозиторий

Код на сервер попадает **клоном с GitHub**, не копированием всего дерева с диска. Так `update.sh` заберёт ваши коммиты с Windows за одну команду.

Нужны **два разных ключа**. Один ключ нельзя одновременно повесить на аккаунт GitHub и как Deploy key.

| Где | Ключ | Зачем |
| --- | --- | --- |
| Windows | ваш обычный ключ GitHub или HTTPS | `git push` из папки разработки |
| Ubuntu | отдельный **deploy key** только этого сервера | только `git pull`, без записи в репозиторий |

### 3.1. На Windows: создать private-репозиторий и запушить

В [GitHub](https://github.com/new): New repository → **Private** → без README, если проект уже есть локально.

В папке Conductor на Windows (PowerShell):

```powershell
cd C:\path\to\Conductor
git remote -v
```

Если `origin` ещё нет или смотрит не туда:

```powershell
git remote add origin git@github.com:ВЛАДЕЛЕЦ/Conductor.git
# или, если origin уже есть:
git remote set-url origin git@github.com:ВЛАДЕЛЕЦ/Conductor.git
git push -u origin HEAD
```

`.env` в git не попадает (он в `.gitignore`). На сервер секреты повезете FileZilla.

Проверьте в браузере, что репозиторий **private** и в нём есть `docker-compose.yml`.

### 3.2. На Ubuntu: ключ только для чтения

Репозитория на сервере ещё нет — команды ниже самодостаточные, скрипт из клона не нужен.

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/conductor_deploy -N "" -C "conductor-deploy"
chmod 600 ~/.ssh/conductor_deploy
cat >> ~/.ssh/config <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/conductor_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
cat ~/.ssh/conductor_deploy.pub
```

Последняя команда печатает одну строку `ssh-ed25519 AAAA...`. Скопируйте её целиком.

### 3.3. Вставить ключ в GitHub

1. Репозиторий → **Settings** → **Deploy keys** → **Add deploy key**.
2. Title: `conductor-server`.
3. Key: вставьте строку с сервера.
4. **Allow write access** — **выкл**. Серверу запись не нужна.
5. Add key.

Проверка на Ubuntu:

```bash
ssh -T git@github.com
```

Ожидается что-то вроде: `Hi ВЛАДЕЛЕЦ/Conductor! You've successfully authenticated, but GitHub does not provide shell access.` Для deploy key часто добавлено, что доступ read-only. Вопрос про fingerprint GitHub — `yes`.

Если `Permission denied` — ключ не тот, в GitHub вставили приватный файл или включили чужой `IdentityFile`.

### 3.4. Клон в `/opt/conductor`

```bash
sudo mkdir -p /opt
sudo git clone git@github.com:ВЛАДЕЛЕЦ/Conductor.git /opt/conductor
sudo chown -R "$USER:$USER" /opt/conductor
cd /opt/conductor
git remote -v
```

Должен быть `git@github.com:ВЛАДЕЛЕЦ/Conductor.git`. Не HTTPS с паролем — иначе каждый `pull` будет мучить токеном.

Если клон уже сделали по HTTPS:

```bash
cd /opt/conductor
git remote set-url origin git@github.com:ВЛАДЕЛЕЦ/Conductor.git
```

Починить ключ и `~/.ssh/config` после того, как клон уже лежит на диске:

```bash
./deploy/ubuntu-24.04/setup-deploy-key.sh
```

---

## 4. Bootstrap: Docker, systemd, firewall

Один скрипт ставит Docker Engine и Compose из [официального репозитория](https://docs.docker.com/engine/install/ubuntu/), создаёт `.env`, каталоги `data/` и `backups/`, автозагрузку и ночной бэкап. На 1 ГБ RAM добавляет swap.

```bash
cd /opt/conductor
sudo bash deploy/ubuntu-24.04/bootstrap.sh --ufw
```

`--ufw` разрешает **OpenSSH (22)** и включает firewall. FileZilla ходит по тому же 22 (SFTP). Порты 5432 и 8080 снаружи не открываются.

Скрипт **сгенерирует** `POSTGRES_PASSWORD`, если в `.env` ещё `change-me`. Не затирайте его без нужды: смена пароля без пересоздания тома ломает вход в уже созданную базу.

После добавления в группу `docker` **закройте SSH и откройте снова** (`ssh conductor`). Иначе `docker` пишет `permission denied`.

```bash
groups
docker version
docker compose version
```

В `groups` должна быть `docker`. В той же сессии без перелогина: `newgrp docker`.

---

## 5. FileZilla и готовый `.env`

Код — через git. FileZilla — чтобы **перетащить уже заполненный `.env`** с Windows и иногда забрать дамп из `backups/`. Протокол только **SFTP** (это SSH), обычный FTP (порт 21) не включайте.

### 5.1. Поставить FileZilla

Скачайте клиент с [filezilla-project.org](https://filezilla-project.org/). На Windows в мастере сайта:

| Поле | Значение |
| --- | --- |
| Протокол | **SFTP — SSH File Transfer Protocol** |
| Хост | тот же IP, что в `ssh conductor` |
| Порт | **22** |
| Тип входа | **Файл ключа** (Key file) |
| Пользователь | тот же, что `User` в SSH config (`ubuntu` / `conductor`) |
| Файл ключа | `C:\Users\<вы>\.ssh\conductor` |

FileZilla может предложить сконвертировать ключ в `.ppk` — согласитесь, рядом появится `conductor.ppk`. Приватный `.ppk` тоже никуда не отправляйте.

Полезные пути в сайте:

- удалённый каталог по умолчанию: `/opt/conductor`
- локальный: папка репозитория на Windows

### 5.2. Что перетаскивать

Слева (Windows) → справа (сервер), **только**:

| С Windows | На сервер | После переноса |
| --- | --- | --- |
| `.env` | `/opt/conductor/.env` | права, см. ниже |
| `.env.test` | `/opt/conductor/.env.test` | если нужен второй бот |
| свой `automod.yml` | `/opt/conductor/data/automod.yml` | кнопка «Перечитать файл» в Discord |

**Не перетаскивайте:** `.venv`, `__pycache__`, `.git`, `data/conductor.db`, `data/backups/*.db`, `.idea`. Код обновляется git'ом.

Справа → налево удобно забирать `backups/conductor-*.sql.gz`.

### 5.3. Пароль Postgres при перетаскивании `.env`

Два безопасных варианта.

**Ещё не запускали стек.** Можно перетащить весь `.env` с Windows, включая свой `POSTGRES_PASSWORD`. Первый `start.sh` создаст базу уже с этим паролем.

**Bootstrap уже сгенерировал пароль, контейнер postgres хоть раз стартовал.** Пароль внутри тома уже зафиксирован. Тогда:

1. На сервере скопируйте текущий пароль: `grep POSTGRES_PASSWORD /opt/conductor/.env`
2. Перетащите Windows-`.env`.
3. Верните в файле **серверный** `POSTGRES_PASSWORD` (или сразу поправьте локальную копию до отправки).

Иначе Postgres не пустит, а токены будут на месте. Если пароль потерян и данных жалко нет: `docker compose down -v` сотрёт том, потом старт с паролем из `.env`.

### 5.4. После любой заливки через FileZilla

FileZilla часто ставит `.env` права `664` — так читать его может лишний процесс. Сразу в SSH:

```bash
cd /opt/conductor
./deploy/ubuntu-24.04/fix-perms.sh
# если скрипт не может сменить владельца:
sudo ./deploy/ubuntu-24.04/fix-perms.sh
```

Проверка:

```bash
ls -l /opt/conductor/.env
# ожидается: -rw-------  и ваш пользователь
```

С Windows без FileZilla то же самое:

```powershell
scp $env:USERPROFILE\path\to\.env conductor:/opt/conductor/.env
```

затем на сервере `fix-perms.sh`.

---

## 6. Права папок, файлов и Docker

Выровнять одной командой: `sudo ./deploy/ubuntu-24.04/fix-perms.sh`.

### 6.1. Зачем это

- `.env` с токенами не должен читаться «всеми».
- Контейнер бота работает как пользователь **uid 1000** (`conductor` внутри образа) и пишет в примонтированный `./data`.
- На типичном Ubuntu первый пользователь тоже **uid 1000** — тогда `data/automod.yml` правится без `sudo`.
- Сокет `/var/run/docker.sock` **не** делают `666`. Доступ — членством в группе `docker`.

Проверка uid:

```bash
id -u
# желательно 1000
```

### 6.2. Нормальная картина

| Путь | Владелец | Права | Кто трогает |
| --- | --- | --- | --- |
| `/opt/conductor` | ваш пользователь | `755` | git, вы |
| `/opt/conductor/.env` | вы | `600` | вы, FileZilla, Compose читает при `up` |
| `/opt/conductor/.env.test` | вы | `600` | второй контейнер |
| `/opt/conductor/data/` | uid 1000 (вы, если `id -u` = 1000) | `755` | контейнер `bot` + вы |
| `/opt/conductor/data/automod.yml` | uid 1000 | `644` | вы и кнопка «Перечитать» |
| `/opt/conductor/data-test/` | uid 1000 | `755` | контейнер `bot-test` |
| `/opt/conductor/backups/` | вы | `755` | `backup.sh`, FileZilla |
| `~/.ssh` | вы | `700` | SSH и git |
| `~/.ssh/authorized_keys` | вы | `600` | вход с Windows |
| `~/.ssh/conductor_deploy` | вы | `600` | `git pull` |
| `/var/run/docker.sock` | root:`docker` | `660` | кто в группе `docker` |

Том Postgres живёт **внутри Docker** (`postgres_data`), не в `/opt/conductor`. Его права руками не правят.

### 6.3. Типичные поломки прав

| Симптом | Причина | Что сделать |
| --- | --- | --- |
| `permission denied` у `docker` | не в группе или не перелогинились | `groups`; новый `ssh conductor` |
| `.env` виден как `-rw-rw-r--` | залили FileZilla | `fix-perms.sh` |
| нельзя сохранить `automod.yml` | контейнер сменил владельца на 1000, ваш uid другой | `sudo nano data/automod.yml` или пользователь с uid 1000 |
| `git pull` спрашивает пароль | нет deploy key / не тот `IdentityFile` | §3 и `setup-deploy-key.sh` |
| `Permission denied (publickey)` с Windows | на сервере нет вашей `.pub` | снова §1.4 |

Не выполняйте `chmod -R 777 /opt/conductor` и не открывайте Docker «на всех».

---

## 7. Проверка `.env` и первый запуск

Минимум в `/opt/conductor/.env` (после FileZilla или правки `nano`):

```
BOT_MODE=live
DISCORD_TOKEN=токен-основного-приложения
GUILD_IDS=id-общедоступного-сервера
TEST_DISCORD_TOKEN=токен-тестового-приложения
TEST_GUILD_IDS=id-тестового-сервера
OWNER_USER_IDS=ваш-discord-id
POSTGRES_PASSWORD=пароль-этого-сервера
TZ=Europe/Moscow
```

Оба токена в одном файле. `BOT_MODE` выбирает процесс:

- `live` — основной бот;
- `test` или `beta` — тестовый.

Оба сразу из одного файла не стартуют. Второй контейнер — §13.

```bash
cd /opt/conductor
./deploy/ubuntu-24.04/fix-perms.sh
./deploy/ubuntu-24.04/check-env.sh
./deploy/ubuntu-24.04/start.sh
```

Первая сборка качает Python и Postgres — несколько минут.

```bash
./deploy/ubuntu-24.04/status.sh --logs
```

В логе — `preflight` (режим и id) и `ready`. `Ctrl+C` закрывает только просмотр.

Автозагрузка уже включена (`conductor.service`):

```bash
sudo systemctl start conductor
```

---

## 8. Проверка в Discord

Команды видны на сервере из списка **запущенного** `BOT_MODE`. Пишет владелец приложения (или id из `OWNER_USER_IDS`).

1. Подождите около минуты: слэш-команды публикуются при старте, если дерево изменилось.
2. `/diagnose` → **Настройка**: интенты, права, позиция роли — без красного.
3. Есть свои каналы и роли — `/admin panel` → **Каркас** → **Сопоставить существующие**. Бот запомнит id и ничего не переименует.
4. `/admin panel` → **Каркас** → **Собрать сервер**. Недостающие категории, каналы и роли бот создаёт сам, вручную ничего создавать не нужно.
5. Если отчёт сборки говорит, что Discord закрыл создание, — это не ошибка: Discord пропускает 12 ролей за 10 минут, а каркасу нужно больше. Повторите сборку через указанное время, повтор безопасен.
6. `/admin panel` → **Каркас** → **Витрины** → **Отправить все недостающие**.
7. `/diagnose` → **Готовность**: «сервер готов к работе».

Пока ранг Специального никому не привязан, команды бота доступны администраторам Discord. Первая сборка выдаёт Специального владельцу сервера, и с этого момента доступ идёт только по рангам. Если роль Специального потом удалят, бот при старте сам снимет замок.

Подробно — [docs/SETUP.md](../../docs/SETUP.md) §10–12. Лист предрелизной проверки — [docs/ACCEPTANCE.md](../../docs/ACCEPTANCE.md). Роль бота **выше** ролей каркаса. Administrator в инвайте не ставьте.

---

## 9. Повседневные команды

Все скрипты — из `/opt/conductor`. С Windows: `ssh conductor`, затем эти команды.

| Действие | Команда |
| --- | --- |
| Статус | `./deploy/ubuntu-24.04/status.sh` |
| Логи бота | `./deploy/ubuntu-24.04/status.sh --logs` |
| Запуск / пересборка | `./deploy/ubuntu-24.04/start.sh` |
| Стоп (том базы целый) | `./deploy/ubuntu-24.04/stop.sh` |
| Рестарт только бота | `docker compose restart bot` |
| Обновление кода с GitHub | `./deploy/ubuntu-24.04/update.sh` |
| Права после FileZilla | `./deploy/ubuntu-24.04/fix-perms.sh` |
| Проверка `.env` | `./deploy/ubuntu-24.04/check-env.sh` |
| Снимок базы | `./deploy/ubuntu-24.04/backup.sh` |
| Восстановить дамп | `./deploy/ubuntu-24.04/restore.sh backups/conductor-….sql.gz` |
| Ревизия схемы | `docker compose exec bot conductor db current` |
| Консоль Postgres | `docker compose exec postgres psql -U conductor -d conductor` |
| Правила автомода | `nano data/automod.yml`, затем «Перечитать файл» в `#автомод` |
| Снять блокировки создания | в Discord: `/owner limits` |

`stop.sh` не удаляет том Postgres. Стереть базу: `docker compose down -v` или с остановленным ботом `python scripts/wipe_data.py --yes`.

---

## 10. Как выкатывать правки с Windows

Обычный день разработки:

1. На Windows правите код, проверяете локально.
2. Коммит и push в **тот же** private-репозиторий (ветка, которую клонировали на сервере — чаще `main`):

   ```powershell
   git add -A
   git status
   git commit -m "кратко зачем"
   git push
   ```

3. На сервере:

   ```bash
   ssh conductor
   cd /opt/conductor
   ./deploy/ubuntu-24.04/update.sh
   ```

`update.sh` делает `git pull --ff-only` и `docker compose up -d --build`. Том Postgres не трогается. `.env` git не перезапишет.

Если `pull` ругается на локальные правки на сервере — их быть не должно. Код меняйте на Windows. Исключение: `.env` и `data/automod.yml`.

Откат кода:

```bash
./deploy/ubuntu-24.04/stop.sh
git checkout <тег-или-коммит>
./deploy/ubuntu-24.04/start.sh
```

---

## 11. Резервные копии

Ночной снимок: timer `conductor-backup.timer` около 04:00.

```
/opt/conductor/backups/conductor-ГГГГММДД-ЧЧММСС.sql.gz
/opt/conductor/backups/automod-ГГГГММДД-ЧЧММСС.yml
```

Последние 14 дампов. Таймер: `systemctl list-timers conductor-backup.timer`.

С Windows забрать файл:

```powershell
scp conductor:/opt/conductor/backups/conductor-YYYYMMDD-HHMMSS.sql.gz .
```

или перетащить из FileZilla каталог `backups/`.

Восстановление на сервере:

```bash
cd /opt/conductor
./deploy/ubuntu-24.04/restore.sh backups/conductor-YYYYMMDD-HHMMSS.sql.gz
```

Бот на это время останавливается. Рядом лежащий `automod-…yml` с той же меткой вернётся в `data/`. В дампе **нет** `.env` — токены храните у себя.

---

## 12. После ребута

С Windows: `ssh conductor`, затем:

```bash
sudo systemctl status conductor
./deploy/ubuntu-24.04/status.sh
```

Если контейнеров нет:

```bash
sudo systemctl enable --now docker conductor
./deploy/ubuntu-24.04/start.sh
```

---

## 13. Второй бот одновременно

Один `.env` запускает только выбранный `BOT_MODE`. Оба онлайн сразу — второй файл, его тоже можно перетащить FileZilla:

```bash
cd /opt/conductor
# или залить .env.test с Windows
```

В `.env.test`:

```
BOT_MODE=test
DISCORD_TOKEN=токен-тестового-приложения
GUILD_IDS=id-тестового-сервера
```

Основной `.env` оставьте с `BOT_MODE=live`. Затем:

```bash
./deploy/ubuntu-24.04/fix-perms.sh
docker compose --profile test up -d
docker compose logs -f bot-test
```

Данные тестового контейнера — `./data-test`. База Postgres общая, строки разделены по `guild_id`.

Два бота на одном сервере делят бюджет недопустимых запросов Discord (10 000 за 10 минут на IP). Не поднимайте оба «впритык» к лимитам. При сбоях в `.env` / `.env.test` поставьте `RATE_PROFILE=safe` и перезапустите контейнер. Профиль, глобальный темп и потолок нажатий — ключи `RATE_PROFILE`, `GLOBAL_RPS_TARGET`, `OUTBOUND_CONCURRENCY`, `MAX_RATELIMIT_WAIT`, `INVALID_REQUEST_STOP`, `INTERACTION_LIMIT_PER_MINUTE` (см. `.env.example`).

---

## 14. Чеклист

**Windows**

- [ ] `ssh conductor` входит по ключу, пароль сервера не спрашивает
- [ ] FileZilla: SFTP, порт 22, тот же ключ
- [ ] Репозиторий на GitHub **private**, `git push` с Windows проходит

**Машина**

- [ ] Ubuntu 24.04, не snap Docker
- [ ] `groups` содержит `docker`
- [ ] UFW: OpenSSH, 5432 снаружи закрыт
- [ ] Клон: `git@github.com:…`, не HTTPS
- [ ] `ssh -T git@github.com` с сервера успешен
- [ ] `/opt/conductor/.env` права `600`
- [ ] `POSTGRES_PASSWORD` не `change-me` и совпадает с живым томом
- [ ] `check-env.sh` проходит
- [ ] `id -u` = 1000 или вы знаете, что `data/` правите через sudo

**Discord**

- [ ] Два приложения, Public Bot выкл
- [ ] Server Members и Message Content включены
- [ ] Каждый бот только на своём сервере
- [ ] Роль бота выше каркаса

**Процесс**

- [ ] `status.sh`: `bot` и `postgres` healthy / running
- [ ] В логе `ready`
- [ ] `/diagnose` без красного в «Настройка»
- [ ] `conductor.service` enabled
- [ ] `conductor-backup.timer` в `systemctl list-timers`

---

## 15. Частые ошибки

| Симптом | Что делать |
| --- | --- |
| Windows: `Permission denied (publickey)` | ключ не попал в `authorized_keys`; повторите §1.4 |
| Windows: `Connection timed out` | облачный firewall / UFW / неверный IP; консоль в панели хостера |
| FileZilla: «не удалось подключиться» | протокол SFTP, порт 22, не FTP; тот же пользователь, что у SSH |
| FileZilla просит пароль | тип входа «Файл ключа», выбран `conductor` или `.ppk` |
| `git clone` / `pull`: `Permission denied` | deploy key не добавлен или вставлен приватный ключ; §3 |
| `git pull` хочет пароль GitHub | remote всё ещё HTTPS: `git remote set-url origin git@…` |
| `permission denied` у `docker` | перелогиниться; `groups` |
| `snap` / странный Compose | `sudo snap remove docker`, снова `bootstrap.sh` |
| `Set POSTGRES_PASSWORD in .env` | ключа нет или залили пустой файл |
| `нет настоящего DISCORD_TOKEN` | плейсхолдер; при `BOT_MODE=test` нужен `TEST_DISCORD_TOKEN` |
| `нужен список серверов` | `GUILD_IDS` / `TEST_GUILD_IDS` |
| Postgres не пускает после заливки `.env` | затёрли сгенерированный пароль; §5.3 |
| Контейнер `unhealthy` | `status.sh --logs` |
| `$'\r': command not found` | CRLF в `.sh`; в репозитории задан LF, `git pull` заново |
| Бот сразу выходит с сервера Discord | id нет в списке этого `BOT_MODE` |
| Слэш-команд нет | не тот бот / не тот сервер; или подождать sync |
| `/diagnose`: файл правил не найден | нет `data/automod.yml` |
| Бот не в сети, timeout | исходящий 443, DNS, нужен IPv4 |
| После ребута бота нет | `sudo systemctl enable --now docker conductor` |
| Сборка падает на 1 ГБ RAM | `swapon --show`; bootstrap должен был сделать swap |
| `chmod 777` «чтобы заработало» | отмените, запустите `fix-perms.sh` |

Разбор прав и интентов на сервере Discord — `/diagnose`.

---

## 16. Состав пакета

| Файл | Назначение |
| --- | --- |
| `bootstrap.sh` | пустая машина: пакеты, Docker, `.env`, systemd, UFW |
| `install-docker.sh` | только Docker Engine + Compose |
| `setup-deploy-key.sh` | ключ сервера для `git pull` (когда клон уже есть) |
| `fix-perms.sh` | владельцы и права после FileZilla / Docker |
| `check-env.sh` | проверка `.env` без запуска |
| `start.sh` | сборка и `docker compose up` |
| `stop.sh` | остановка, том базы целый |
| `update.sh` | `git pull` и пересборка |
| `status.sh` | systemd + контейнеры, `--logs` — хвост |
| `backup.sh` | дамп Postgres + копия `automod.yml` |
| `restore.sh` | откат из дампа |
| `systemd/conductor.service` | автозагрузка стека |
| `systemd/conductor-backup.timer` | ночной снимок около 04:00 |
| `examples/windows-ssh-config` | шаблон для `C:\Users\<вы>\.ssh\config` |
| `examples/github-deploy-ssh-config` | шаблон `Host github.com` на Ubuntu |
