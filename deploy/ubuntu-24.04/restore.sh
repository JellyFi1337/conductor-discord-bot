#!/usr/bin/env bash
# Восстанавливает Postgres из дампа .sql.gz. Бот на это время останавливается.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo
require_docker
load_postgres_env

confirm=1
if [ "${1:-}" = "--yes" ]; then
  confirm=0
  shift
fi

dump="${1:-}"
[ -n "$dump" ] || die "Использование: $0 [--yes] backups/conductor-ГГГГММДД-ЧЧММСС.sql.gz"
[ -f "$dump" ] || die "Нет файла: $dump"

echo "Восстановление из $dump"
if [ "$confirm" -eq 1 ]; then
  echo "Контейнер bot будет остановлен. Продолжить? [y/N]"
  read -r answer
  case "$answer" in
    y | Y | yes | YES) ;;
    *)
      echo "Отменено."
      exit 2
      ;;
  esac
fi

compose up -d postgres
wait_postgres
compose stop bot bot-test >/dev/null 2>&1 || true

if [[ "$dump" == *.gz ]]; then
  gunzip -c "$dump" | compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
else
  compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$dump"
fi

stamp="$(basename "$dump")"
stamp="${stamp#conductor-}"
stamp="${stamp%.sql.gz}"
stamp="${stamp%.sql}"
automod="$REPO_ROOT/backups/automod-$stamp.yml"
if [ -f "$automod" ]; then
  mkdir -p "$REPO_ROOT/data"
  cp "$automod" "$REPO_ROOT/data/automod.yml"
  echo "Восстановлен data/automod.yml из $automod"
fi

compose up -d bot
echo "Готово. Проверьте: $PACK_DIR/status.sh --logs"
