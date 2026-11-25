#!/usr/bin/env bash
# Выравнивает владельцев и права после FileZilla, git clone и Docker.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_repo

if is_root; then
  owner="$(deploy_user)"
  [ -n "$owner" ] || die "Не понял, кому отдать файлы. Запустите без sudo от своего пользователя или задайте CONDUCTOR_USER."
else
  owner="$USER"
fi

home="$(getent passwd "$owner" | cut -d: -f6)"
uid="$(id -u "$owner")"
gid="$(id -g "$owner")"

echo "Владелец клона: $owner (uid $uid)  каталог: $REPO_ROOT"

if is_root; then
  chown -R "$owner:$owner" "$REPO_ROOT"
else
  echo "Без sudo владельца всего дерева не меняю — только права доступных файлов."
fi

chmod 0755 "$REPO_ROOT"
find "$PACK_DIR" -maxdepth 1 -type f -name '*.sh' -exec chmod 0755 {} \;

for secret in "$REPO_ROOT/.env" "$REPO_ROOT/.env.test" "$REPO_ROOT/.env.beta"; do
  if [ -f "$secret" ]; then
    if is_root; then
      chown "$owner:$owner" "$secret"
    fi
    chmod 600 "$secret"
    echo "секреты: $secret → 600"
  fi
done

mkdir -p "$REPO_ROOT/data" "$REPO_ROOT/data-test" "$REPO_ROOT/backups"
# В контейнере пользователь conductor — uid 1000. Так же часто устроен
# первый пользователь Ubuntu. Иначе контейнер перезапишет владельца data/.
if [ "$uid" = "1000" ]; then
  if is_root; then
    chown -R "$owner:$owner" "$REPO_ROOT/data" "$REPO_ROOT/data-test" "$REPO_ROOT/backups"
  fi
  echo "data/: владелец $owner (совпадает с uid контейнера 1000)"
else
  if is_root; then
    chown -R 1000:1000 "$REPO_ROOT/data" "$REPO_ROOT/data-test" || true
    chown -R "$owner:$owner" "$REPO_ROOT/backups"
  fi
  echo "Внимание: uid $owner = $uid, а контейнер пишет в data/ как 1000."
  echo "Править automod.yml: sudo nano $REPO_ROOT/data/automod.yml"
fi
chmod 0755 "$REPO_ROOT/data" "$REPO_ROOT/data-test" "$REPO_ROOT/backups" 2>/dev/null || true

if [ -d "$home/.ssh" ]; then
  if is_root; then
    chown -R "$owner:$owner" "$home/.ssh"
  fi
  chmod 700 "$home/.ssh"
  [ -f "$home/.ssh/authorized_keys" ] && chmod 600 "$home/.ssh/authorized_keys"
  [ -f "$home/.ssh/config" ] && chmod 600 "$home/.ssh/config"
  [ -f "$home/.ssh/conductor_deploy" ] && chmod 600 "$home/.ssh/conductor_deploy"
  [ -f "$home/.ssh/conductor_deploy.pub" ] && chmod 644 "$home/.ssh/conductor_deploy.pub"
  echo "SSH: $home/.ssh → 700, ключи 600"
fi

if id -nG "$owner" | grep -qw docker; then
  echo "группа docker: $owner уже в ней"
else
  echo "группа docker: $owner ещё нет. После bootstrap перелогиньтесь по SSH."
fi

echo "Готово. Сокет Docker не трогал — его не делают 666."
