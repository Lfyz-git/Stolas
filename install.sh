#!/bin/sh
# Run from a checkout: sh install.sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$(uname -s)" != Linux ]; then
    echo 'Stolas устанавливается на Linux-хосте.' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo 'Установите Python 3.10+ и повторите запуск.' >&2
        exit 1
    fi
    echo 'Для мастера нужен Python 3.10+. Установить python3 через apt? [Y/n]'
    read -r answer
    case "$answer" in n|N|no) exit 1 ;; esac
    if [ "$(id -u)" = 0 ]; then
        apt-get update
        apt-get install -y python3
    else
        sudo apt-get update
        sudo apt-get install -y python3
    fi
fi
exec python3 tools/deploy.py "$@"
