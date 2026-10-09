#!/bin/sh
# Download a source archive and hand over to the interactive Linux installer.
# Keep execution inside a function so an incomplete download cannot run a prefix.
main() {
    set -eu
    umask 022
    target=""
    ref=main
    workdir=""

    fail() { printf 'Stolas: %s\n' "$*" >&2; exit 1; }
    usage() {
        cat <<'EOF'
Установка Stolas без Git (Linux).
  --dir PATH          каталог установки (по умолчанию: $HOME/stolas)
  --ref REF           ветка, тег или SHA коммита (по умолчанию: main)
  --configure-only    только подготовить настройки, без Docker и теста
  --help              показать справку
EOF
    }
    configure_only=false
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir|--ref)
                [ "$#" -ge 2 ] && [ -n "$2" ] || fail "После $1 требуется значение."
                case "$1" in --dir) target=$2 ;; --ref) ref=$2 ;; esac
                shift 2 ;;
            --configure-only) configure_only=true; shift ;;
            --help|-h) usage; exit 0 ;;
            *) fail "Неизвестный параметр: $1" ;;
        esac
    done
    [ "$(uname -s)" = Linux ] || fail 'Запустите загрузчик на целевом Linux-хосте.'
    case "$ref" in ''|*[!a-zA-Z0-9._-]*|.|..) fail 'REF должен быть именем без / или SHA коммита.' ;; esac
    command -v tar >/dev/null 2>&1 || fail 'Установите tar и повторите запуск.'
    if command -v curl >/dev/null 2>&1; then
        downloader=curl
    elif command -v wget >/dev/null 2>&1; then
        downloader=wget
    else
        fail 'Установите curl или wget и повторите запуск.'
    fi

    # sh initially reads this script from the pipe. The wizard must read the TTY.
    if [ ! -t 0 ]; then
        ( : </dev/tty ) 2>/dev/null || fail 'Нужен интерактивный терминал (SSH: используйте ssh -t).'
        exec </dev/tty
    fi
    if [ -z "$target" ]; then
        [ -n "${HOME:-}" ] || fail 'Укажите каталог через --dir.'
        printf 'Каталог установки [%s/stolas]: ' "$HOME"
        IFS= read -r target || fail 'Ввод прерван.'
        target=${target:-"$HOME/stolas"}
    fi
    case "$target" in /*) ;; *) target="$PWD/$target" ;; esac
    # Resolve the parent before allocating temporary files or moving anything.
    target=${target%/}
    [ -n "$target" ] || fail 'Нельзя устанавливать в корень файловой системы.'
    basename=${target##*/}
    case "$basename" in ''|.|..) fail 'Укажите отдельный новый каталог.' ;; esac
    parent=${target%/*}
    parent=${parent:-/}
    mkdir -p -- "$parent"
    parent=$(CDPATH= cd -- "$parent" && pwd -P)
    target="$parent/$basename"
    if [ -e "$target" ] || [ -L "$target" ]; then
        printf 'Каталог уже существует: %s\n' "$target" >&2
        printf 'Для повторной настройки выполните: cd "%s" && sh install.sh\n' "$target" >&2
        fail 'Существующая установка не перезаписана. Для новой выберите другой --dir.'
    fi
    workdir=$(mktemp -d "$parent/.stolas-download.XXXXXX")
    trap 'if [ -n "$workdir" ]; then rm -rf -- "$workdir"; fi' 0
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    archive="$workdir/source.tar.gz"
    url="https://codeload.github.com/Lfyz-git/Stolas/tar.gz/$ref"
    printf 'Загрузка Stolas (%s)…\n' "$ref"
    if [ "$downloader" = curl ]; then
        curl --fail --location --show-error --silent --proto '=https' --proto-redir '=https' \
            --connect-timeout 15 --max-time 180 --output "$archive" "$url" || \
            fail 'Не удалось загрузить архив. Проверьте сеть, REF и публичный доступ к репозиторию.'
    else
        wget --https-only --timeout=30 --tries=2 -q -O "$archive" "$url" || \
            fail 'Не удалось загрузить архив. Проверьте сеть, REF и публичный доступ к репозиторию.'
    fi
    # Validate the GitHub archive's single root and member paths before extraction.
    tar -tzf "$archive" > "$workdir/members" || fail 'Повреждённый архив.'
    prefix=""
    while IFS= read -r member; do
        case "$member" in /*|..|../*|*/../*|*/..|*\\*) fail 'Недопустимый путь в архиве.' ;; esac
        top=${member%%/*}
        if [ -z "$prefix" ]; then
            case "$top" in Stolas-?*) prefix=$top ;; *) fail 'Неожиданный корневой каталог архива.' ;; esac
        fi
        [ "$top" = "$prefix" ] || fail 'Архив должен содержать один корневой каталог.'
    done < "$workdir/members"
    [ -n "$prefix" ] || fail 'Пустой архив.'
    LC_ALL=C tar -tvzf "$archive" > "$workdir/types"
    while IFS= read -r entry; do
        case "$entry" in -*|d*) ;; *) fail 'В архиве допустимы только обычные файлы и каталоги.' ;; esac
    done < "$workdir/types"
    mkdir "$workdir/unpacked"
    tar -xzf "$archive" -C "$workdir/unpacked" --no-same-owner --no-same-permissions
    source="$workdir/unpacked/$prefix"
    for file in install.sh tools/install.py agent/config.py compose.yaml config/example.json; do
        [ -f "$source/$file" ] && [ ! -L "$source/$file" ] || fail "В архиве отсутствует $file."
    done
    # -T prevents nesting under a concurrently created target; -n never overwrites.
    mv -T -n -- "$source" "$target"
    [ ! -d "$source" ] || fail 'Каталог назначения появился во время загрузки; он не изменён.'
    printf 'Stolas распакован в %s\n' "$target"
    printf 'Запуск интерактивного мастера…\n'
    cd -- "$target"
    if [ "$configure_only" = true ]; then
        sh ./install.sh --configure-only
    else
        sh ./install.sh
    fi
    exit 0
}
main "$@"
