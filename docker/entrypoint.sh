#!/bin/sh
# Готовит /app/data и собирает DATABASE_URL из POSTGRES_*, затем отдаёт процесс
# пользователю conductor. Всё остальное решает сам бот.
set -eu

# В образе пакет живёт в site-packages, поэтому «каталог проекта» боту не подходит:
# каталог данных задаётся явно. Пустое значение из .env тоже заменяется.
DATA_DIR="${DATA_DIR:-/app/data}"
export DATA_DIR
mkdir -p "$DATA_DIR"

# Без файла правил автомод работает на встроенных умолчаниях, а /diagnose
# показывает предупреждение. Первый запуск кладёт эталон, дальше файл — ваш.
if [ ! -f "$DATA_DIR/automod.yml" ] && [ -f /app/defaults/automod.yml.example ]; then
  cp /app/defaults/automod.yml.example "$DATA_DIR/automod.yml"
  echo "entrypoint: положил $DATA_DIR/automod.yml из эталона"
fi
if [ ! -f "$DATA_DIR/automod.yml.example" ] && [ -f /app/defaults/automod.yml.example ]; then
  cp /app/defaults/automod.yml.example "$DATA_DIR/automod.yml.example"
fi
if [ ! -f "$DATA_DIR/permissions.yml.example" ] && [ -f /app/defaults/permissions.yml.example ]; then
  cp /app/defaults/permissions.yml.example "$DATA_DIR/permissions.yml.example"
fi

if [ "$(id -u)" = "0" ]; then
  chown -R conductor:conductor "$DATA_DIR"
fi

if [ -n "${POSTGRES_HOST:-}" ] && [ -n "${POSTGRES_PASSWORD:-}" ]; then
  DATABASE_URL="$(
    python - <<'PY'
import os
from urllib.parse import quote_plus

user = quote_plus(os.environ.get("POSTGRES_USER", "conductor"))
password = quote_plus(os.environ["POSTGRES_PASSWORD"])
host = os.environ["POSTGRES_HOST"]
port = os.environ.get("POSTGRES_PORT", "5432")
name = quote_plus(os.environ.get("POSTGRES_DB", "conductor"))
print(f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}")
PY
  )"
  export DATABASE_URL
fi

if [ -z "${AUTO_MIGRATE:-}" ]; then
  export AUTO_MIGRATE=true
fi

if [ "$(id -u)" = "0" ]; then
  exec gosu conductor "$@"
fi

exec "$@"
