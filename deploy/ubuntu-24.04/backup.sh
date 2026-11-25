#!/usr/bin/env bash
# Снимок Postgres и копия automod.yml. Оставляет последние 14 дампов.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo
require_docker
load_postgres_env

if ! compose ps --status running --services 2>/dev/null | grep -qx postgres; then
  die "Контейнер postgres не запущен. Сначала: $PACK_DIR/start.sh"
fi

stamp="$(date +%Y%m%d-%H%M%S)"
out_dir="$REPO_ROOT/backups"
mkdir -p "$out_dir"
out_file="$out_dir/conductor-$stamp.sql.gz"

compose exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
  | gzip > "$out_file"

if [ -f "$REPO_ROOT/data/automod.yml" ]; then
  cp "$REPO_ROOT/data/automod.yml" "$out_dir/automod-$stamp.yml"
fi

ls -1t "$out_dir"/conductor-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm --
ls -1t "$out_dir"/automod-*.yml 2>/dev/null | tail -n +15 | xargs -r rm --

echo "Снимок записан: $out_file"
