#!/usr/bin/env bash
# Ставит Docker Engine и плагин Compose из официального репозитория Docker.
set -euo pipefail

. "$(dirname "$0")/lib.sh"
require_root
require_ubuntu_24
reject_snap_docker

# shellcheck disable=SC1091
. /etc/os-release
codename="${VERSION_CODENAME:-noble}"

apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version >/dev/null
docker compose version >/dev/null
echo "Docker Engine и Compose установлены."
