#!/usr/bin/env bash
# Обновляет код и пересобирает контейнеры. Том Postgres не трогает.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo
require_docker
reject_snap_docker
check_env

cd "$REPO_ROOT"
if [ -d .git ]; then
  echo "git pull в $REPO_ROOT"
  git pull --ff-only
else
  echo "Это не git-клон — код не обновляю, только пересобираю образ."
fi

compose up -d --build --remove-orphans
compose ps
echo "Готово. Том Postgres на месте."
