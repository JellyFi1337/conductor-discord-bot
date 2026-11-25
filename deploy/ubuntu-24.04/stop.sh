#!/usr/bin/env bash
# Останавливает бота и стек. Том Postgres не удаляет.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo

echo "Останавливаю Conductor в $REPO_ROOT"

if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files conductor.service >/dev/null 2>&1; then
    if is_root; then
      systemctl stop conductor.service || true
    else
      sudo systemctl stop conductor.service || true
    fi
  fi
fi

if docker_ready; then
  compose down --remove-orphans || true
  leftover="$(
    { docker ps -aq --filter name=conductor; docker ps -aq --filter ancestor=conductor:local; } | sort -u
  )" || true
  if [ -n "${leftover:-}" ]; then
    echo "$leftover" | while read -r id; do
      [ -z "$id" ] && continue
      echo "  контейнер $id"
      docker stop --time 20 "$id" >/dev/null 2>&1 || true
      docker rm -f "$id" >/dev/null 2>&1 || true
    done
  fi
else
  echo "Docker недоступен — контейнеры пропускаю."
fi

if command -v pgrep >/dev/null 2>&1; then
  pkill -f '[p]ython[0-9.]* -m conductor' >/dev/null 2>&1 || true
fi

echo "Тома Postgres на месте. Запуск: $PACK_DIR/start.sh"
