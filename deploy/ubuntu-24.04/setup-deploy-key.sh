#!/usr/bin/env bash
# Создаёт read-only ключ сервера для git pull из приватного GitHub.
set -euo pipefail

. "$(dirname "$0")/lib.sh"

if is_root; then
  owner="$(deploy_user)"
  [ -n "$owner" ] || die "Запустите от обычного пользователя или sudo -u USER $0"
  home="$(getent passwd "$owner" | cut -d: -f6)"
  run_as=(sudo -u "$owner")
else
  owner="$USER"
  home="$HOME"
  run_as=()
fi

[ -n "$home" ] || die "Нет домашнего каталога у $owner"
mkdir -p "$home/.ssh"
chmod 700 "$home/.ssh"
if is_root; then
  chown "$owner:$owner" "$home/.ssh"
fi

key="$home/.ssh/conductor_deploy"
if [ ! -f "$key" ]; then
  "${run_as[@]}" ssh-keygen -t ed25519 -f "$key" -N "" -C "conductor-deploy@$(hostname)"
  echo "Создан $key"
else
  echo "Ключ уже есть: $key"
fi
chmod 600 "$key"
[ -f "$key.pub" ] && chmod 644 "$key.pub"
if is_root; then
  chown "$owner:$owner" "$key" "$key.pub"
fi

config="$home/.ssh/config"
marker="Host github.com"
if [ ! -f "$config" ] || ! grep -q "IdentityFile $key" "$config" 2>/dev/null; then
  {
    echo
    echo "$marker"
    echo "  HostName github.com"
    echo "  User git"
    echo "  IdentityFile $key"
    echo "  IdentitiesOnly yes"
  } >> "$config"
  chmod 600 "$config"
  if is_root; then
    chown "$owner:$owner" "$config"
  fi
  echo "В $config добавлен Host github.com"
fi

echo
echo "1. Скопируйте эту строку целиком."
echo "2. GitHub → репозиторий → Settings → Deploy keys → Add deploy key."
echo "3. Title: conductor-server   Key: вставьте   Allow write access: выкл."
echo
cat "$key.pub"
echo
echo "Проверка после добавления ключа:"
echo "  ssh -T git@github.com"
echo "Ожидается: Hi ВЛАДЕЛЕЦ/Conductor! You've successfully authenticated (read only)."
echo
echo "Клон:"
echo "  sudo mkdir -p /opt && sudo git clone git@github.com:ВЛАДЕЛЕЦ/Conductor.git /opt/conductor"
echo "  sudo chown -R $owner:$owner /opt/conductor"
