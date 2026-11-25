#!/usr/bin/env bash
# Статус systemd, контейнеров и при необходимости хвост логов.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo

show_logs=0
if [ "${1:-}" = "--logs" ] || [ "${1:-}" = "-f" ]; then
  show_logs=1
fi

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files conductor.service >/dev/null 2>&1; then
  systemctl is-enabled conductor.service >/dev/null 2>&1 && echo "systemd: conductor.service включён" || echo "systemd: conductor.service выключен"
  systemctl is-active conductor.service >/dev/null 2>&1 && echo "systemd: active" || echo "systemd: inactive"
fi

if docker_ready; then
  echo
  compose ps
  if [ "$show_logs" -eq 1 ]; then
    echo
    compose logs -f --tail=200 bot
  fi
else
  echo "Docker недоступен."
  exit 1
fi
