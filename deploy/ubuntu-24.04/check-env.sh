#!/usr/bin/env bash
# Проверяет .env перед запуском на сервере.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo
check_env
echo "Файл .env выглядит готовым к запуску (BOT_MODE=$(env_get BOT_MODE))."
