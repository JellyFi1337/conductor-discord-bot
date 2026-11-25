#!/usr/bin/env sh
# Совместимость: остановка переехала в deploy/ubuntu-24.04/stop.sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec bash "$ROOT/deploy/ubuntu-24.04/stop.sh"
