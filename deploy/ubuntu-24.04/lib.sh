# Общие функции пакета Ubuntu 24.04. Подключать так: . "$(dirname "$0")/lib.sh"
# shellcheck shell=bash

if [ -n "${CONDUCTOR_LIB_LOADED:-}" ]; then
  return 0
fi
CONDUCTOR_LIB_LOADED=1

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PACK_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
ENV_FILE="$REPO_ROOT/.env"
PLACEHOLDER_TOKENS="your-bot-token changeme token xxx"

die() {
  echo "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Нужна команда: $1"
}

is_root() {
  [ "$(id -u)" -eq 0 ]
}

require_root() {
  is_root || die "Запустите от root: sudo $0 $*"
}

require_repo() {
  [ -f "$COMPOSE_FILE" ] || die "Не найден docker-compose.yml в $REPO_ROOT"
}

require_ubuntu_24() {
  [ -f /etc/os-release ] || die "Не удалось определить дистрибутив."
  # shellcheck disable=SC1091
  . /etc/os-release
  if [ "${ID:-}" != "ubuntu" ]; then
    die "Пакет рассчитан на Ubuntu 24.04. Сейчас: ${PRETTY_NAME:-unknown}"
  fi
  case "${VERSION_ID:-}" in
    24.04) ;;
    *)
      echo "Ожидается Ubuntu 24.04, сейчас ${PRETTY_NAME:-unknown}." >&2
      echo "Продолжать можно на свой риск, официально пакет не проверялся." >&2
      ;;
  esac
}

deploy_user() {
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    printf '%s' "$SUDO_USER"
    return
  fi
  if [ -n "${CONDUCTOR_USER:-}" ] && [ "$CONDUCTOR_USER" != "root" ]; then
    printf '%s' "$CONDUCTOR_USER"
    return
  fi
  if ! is_root && [ -n "${USER:-}" ] && [ "$USER" != "root" ]; then
    printf '%s' "$USER"
    return
  fi
  printf '%s' ""
}

env_get() {
  local key="$1"
  local file="${2:-$ENV_FILE}"
  local line=""
  [ -f "$file" ] || return 0
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

env_set() {
  local key="$1"
  local value="$2"
  local file="${3:-$ENV_FILE}"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

ensure_env_key() {
  local key="$1"
  local value="$2"
  if ! grep -qE "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    echo "В .env добавлен $key."
  fi
}

trim_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]'
}

is_placeholder_token() {
  local value
  value="$(trim_lower "$1")"
  [ -z "$value" ] && return 0
  case " $PLACEHOLDER_TOKENS " in
    *" $value "*) return 0 ;;
  esac
  return 1
}

is_test_mode() {
  case "$(trim_lower "${1:-live}")" in
    test | testing | dev | beta) return 0 ;;
  esac
  return 1
}

compose() {
  docker compose --project-directory "$REPO_ROOT" -f "$COMPOSE_FILE" "$@"
}

docker_ready() {
  command -v docker >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker не установлен. Сначала: sudo bash $PACK_DIR/bootstrap.sh"
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  if [ "$(id -u)" -ne 0 ]; then
    die "Нет доступа к Docker. Выйдите из SSH и зайдите снова (группа docker), либо запустите через sudo."
  fi
  die "Служба Docker не запущена: sudo systemctl start docker"
}

reject_snap_docker() {
  if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then
    die "Установлен snap-пакет docker — он ломает Compose. Удалите: sudo snap remove docker"
  fi
}

load_postgres_env() {
  POSTGRES_USER="$(env_get POSTGRES_USER)"
  POSTGRES_DB="$(env_get POSTGRES_DB)"
  POSTGRES_USER="${POSTGRES_USER:-conductor}"
  POSTGRES_DB="${POSTGRES_DB:-conductor}"
}

check_env() {
  local mode token test_token process_token guilds test_guilds public_id test_id password
  [ -f "$ENV_FILE" ] || die "Нет $ENV_FILE. Скопируйте .env.example и заполните."
  mode="$(env_get BOT_MODE)"
  token="$(env_get DISCORD_TOKEN)"
  test_token="$(env_get TEST_DISCORD_TOKEN)"
  guilds="$(env_get GUILD_IDS)"
  test_guilds="$(env_get TEST_GUILD_IDS)"
  public_id="$(env_get PUBLIC_GUILD_ID)"
  test_id="$(env_get TEST_GUILD_ID)"
  password="$(env_get POSTGRES_PASSWORD)"
  if is_test_mode "$mode" && [ -n "$(printf '%s' "$test_token" | tr -d '[:space:]')" ]; then
    process_token="$test_token"
  else
    process_token="$token"
  fi
  if is_placeholder_token "$process_token"; then
    if is_test_mode "$mode"; then
      die "В .env нет токена тестового бота: TEST_DISCORD_TOKEN."
    fi
    die "В .env нет настоящего DISCORD_TOKEN."
  fi
  if is_test_mode "$mode"; then
    if [ -z "$(printf '%s' "$test_guilds$test_id$guilds" | tr -d '[:space:]')" ]; then
      die "В .env нужен список серверов тестового бота: TEST_GUILD_IDS или TEST_GUILD_ID."
    fi
  elif [ -z "$(printf '%s' "$guilds$public_id" | tr -d '[:space:]')" ]; then
    die "В .env нужен список серверов основного бота: GUILD_IDS или PUBLIC_GUILD_ID."
  fi
  if [ -z "$(printf '%s' "$password" | tr -d '[:space:]')" ] || [ "$password" = "change-me" ]; then
    die "Задайте POSTGRES_PASSWORD в .env (не оставляйте change-me на сервере)."
  fi
}

wait_postgres() {
  local i
  for i in $(seq 1 36); do
    if compose exec -T postgres pg_isready -U "${POSTGRES_USER:-conductor}" -d "${POSTGRES_DB:-conductor}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  die "Postgres не стал готов за 72 секунды. Смотрите: docker compose logs postgres"
}
