#!/bin/sh
# Fresh Ubuntu/Debian hosts only. Invoked by the interactive installer as root.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Нужен root.' >&2; exit 1; }
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo 'Автоустановка Docker: только Ubuntu/Debian.' >&2; exit 1 ;; esac
: "${VERSION_CODENAME:?Не определён codename дистрибутива}"
if command -v docker >/dev/null 2>&1; then
    echo 'Docker уже установлен; установите Compose v2 самостоятельно, затем повторите запуск.' >&2
    exit 1
fi
# Do not remove or replace another container runtime on an existing host.
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
        echo "Обнаружен $package. Настройте Docker/Compose вручную, затем повторите запуск." >&2
        exit 1
    fi
done
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl --fail --show-error --silent --connect-timeout 15 --max-time 60 \
    "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/stolas-docker.asc
chmod 0644 /etc/apt/keyrings/stolas-docker.asc
cat > /etc/apt/sources.list.d/stolas-docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/stolas-docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
