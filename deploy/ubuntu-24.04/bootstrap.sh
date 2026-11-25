#!/usr/bin/env bash
# Готовит пустой Ubuntu 24.04: пакеты, Docker, .env, каталоги, systemd, опционально UFW.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_root
require_ubuntu_24
require_repo
reject_snap_docker

enable_ufw=0
for arg in "$@"; do
  case "$arg" in
    --ufw) enable_ufw=1 ;;
    -h | --help)
      echo "Использование: sudo bash $0 [--ufw]"
      echo "  --ufw  разрешить OpenSSH и включить firewall"
      exit 0
      ;;
    *) die "Неизвестный аргумент: $arg" ;;
  esac
done

owner="$(deploy_user)"
# shellcheck disable=SC1091
. /etc/os-release

echo "Репозиторий: $REPO_ROOT"
echo "Дистрибутив: ${PRETTY_NAME:-Ubuntu}"
if [ -n "$owner" ]; then
  echo "Владелец файлов и группа docker: $owner"
else
  echo "Вы вошли как root. Создайте обычного пользователя и добавьте его в группу docker."
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git ufw openssl openssh-server
systemctl enable --now ssh

if [ -f /usr/share/zoneinfo/Europe/Moscow ]; then
  timedatectl set-timezone Europe/Moscow || true
fi

ensure_swap() {
  local mem_kb
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [ "${mem_kb:-0}" -ge 1500000 ]; then
    return 0
  fi
  if swapon --show | grep -q .; then
    return 0
  fi
  echo "Мало RAM — добавляю swap 1G для сборки образа."
  if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
  if ! grep -qE '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
}

ensure_swap

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker и Compose уже установлены."
  systemctl enable --now docker
else
  echo "Ставлю Docker Engine и Compose plugin."
  bash "$PACK_DIR/install-docker.sh"
fi

if [ -n "$owner" ]; then
  usermod -aG docker "$owner" || true
  echo "Пользователь $owner добавлен в группу docker. Чтобы группа применилась, выйдите из SSH и зайдите снова."
fi

if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_ROOT/.env.example" "$ENV_FILE"
  echo "Создан $ENV_FILE из примера."
fi
ensure_env_key POSTGRES_USER conductor
ensure_env_key POSTGRES_PASSWORD change-me
ensure_env_key POSTGRES_DB conductor
ensure_env_key POSTGRES_PORT 5432
ensure_env_key TZ Europe/Moscow
ensure_env_key BOT_MODE live

if [ "$(env_get POSTGRES_PASSWORD)" = "change-me" ] || [ -z "$(env_get POSTGRES_PASSWORD)" ]; then
  generated="$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)"
  env_set POSTGRES_PASSWORD "$generated"
  echo "Сгенерирован POSTGRES_PASSWORD (латиница и цифры). Он уже в .env — не коммитьте этот файл."
fi

if [ -n "$owner" ]; then
  chown "$owner:$owner" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

mkdir -p "$REPO_ROOT/data" "$REPO_ROOT/data-test" "$REPO_ROOT/backups"
if [ -n "$owner" ]; then
  chown -R "$owner:$owner" "$REPO_ROOT/data" "$REPO_ROOT/data-test" "$REPO_ROOT/backups"
fi

find "$PACK_DIR" -maxdepth 1 -type f -name '*.sh' -exec chmod 0755 {} \;

install_unit() {
  local src="$1"
  local name
  name="$(basename "$src")"
  sed "s|__WORKING_DIRECTORY__|$REPO_ROOT|g" "$src" > "/etc/systemd/system/$name"
}

install_unit "$PACK_DIR/systemd/conductor.service"
install_unit "$PACK_DIR/systemd/conductor-backup.service"
install_unit "$PACK_DIR/systemd/conductor-backup.timer"
systemctl daemon-reload
systemctl enable conductor.service
systemctl enable --now conductor-backup.timer

if [ "$enable_ufw" -eq 1 ]; then
  ufw allow OpenSSH
  ufw --force enable
  echo "UFW включён, открыт только OpenSSH. Порты 5432 и 8080 наружу не публикуйте."
fi

bash "$PACK_DIR/fix-perms.sh" || true

echo
echo "Готово. Дальше:"
echo "  1. Заполните $ENV_FILE токенами и id серверов (или перетащите готовый файл через FileZilla)."
echo "     Пароль Postgres уже сгенерирован — не затирайте его без нужды."
echo "  2. Выйдите из SSH и зайдите снова (группа docker)."
echo "  3. Права:    $PACK_DIR/fix-perms.sh"
echo "  4. Проверка: $PACK_DIR/check-env.sh"
echo "  5. Запуск:   $PACK_DIR/start.sh"
echo "  6. Статус:   $PACK_DIR/status.sh --logs"
echo "Инструкция: $PACK_DIR/README.md"
