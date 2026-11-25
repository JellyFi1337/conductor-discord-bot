#!/usr/bin/env bash
# Совместимость: полная установка переехала в deploy/ubuntu-24.04/bootstrap.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec bash "$ROOT/deploy/ubuntu-24.04/bootstrap.sh" "$@"
