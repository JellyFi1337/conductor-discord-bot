#!/usr/bin/env bash
# Собирает образы и поднимает postgres + bot.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo
require_docker
reject_snap_docker
check_env

echo "Собираю образы и поднимаю стек в $REPO_ROOT"
compose up -d --build --remove-orphans
echo
compose ps
echo
echo "Логи бота: $PACK_DIR/status.sh --logs"
echo "Или: docker compose -f $COMPOSE_FILE logs -f --tail=200 bot"
