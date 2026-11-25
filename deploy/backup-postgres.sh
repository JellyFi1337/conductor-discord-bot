#!/usr/bin/env bash
# Совместимость: снимок переехал в deploy/ubuntu-24.04/backup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "$ROOT/deploy/ubuntu-24.04/backup.sh" "$@"
