#!/bin/sh
# Download a source archive and hand over to the interactive Linux installer.
# Keep execution inside a function so an incomplete download cannot run a prefix.
main() {
    set -eu
    umask 022
    target=""
    ref=v0.2.0
    expected=""
    action=""
    workdir=""

    fail() { printf 'Stolas: %s\n' "$*" >&2; exit 1; }
    usage() {
        cat <<'EOF'
Установка Stolas без Git (Linux).
  --dir PATH          каталог установки (по умолчанию: $HOME/stolas)
  --ref REF           релиз, ветка или SHA (по умолчанию: v0.2.0)
  --sha256 HASH       ожидаемый SHA-256 архива при установке SHA/main
  --action ACTION     reconfigure/update/rollback/cancel для существующей установки
  --configure-only    только подготовить настройки, без Docker и теста
  --help              показать справку
EOF
    }
    configure_only=false
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir|--ref|--sha256|--action)
                [ "$#" -ge 2 ] && [ -n "$2" ] || fail "После $1 требуется значение."
                case "$1" in --dir) target=$2 ;; --ref) ref=$2 ;; --sha256) expected=$2 ;; --action) action=$2 ;; esac
                shift 2 ;;
            --configure-only) configure_only=true; shift ;;
            --help|-h) usage; exit 0 ;;
            *) fail "Неизвестный параметр: $1" ;;
        esac
    done
    [ "$(uname -s)" = Linux ] || fail 'Запустите загрузчик на целевом Linux-хосте.'
    case "$ref" in ''|*[!a-zA-Z0-9._-]*|.|..) fail 'REF должен быть именем без / или SHA коммита.' ;; esac
    command -v tar >/dev/null 2>&1 || fail 'Установите tar и повторите запуск.'
    command -v sha256sum >/dev/null 2>&1 || fail 'Установите sha256sum (coreutils).'
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
    # Staging must not require write access to /opt when /opt/stolas is user-owned.
    workdir=$(mktemp -d "${TMPDIR:-/tmp}/stolas-download.XXXXXX")
    trap 'status=$?; if [ "$status" = 0 ]; then rm -rf -- "$workdir"; else printf "exit_code=%s\n" "$status" > "$workdir/diagnostic.txt"; printf "Диагностика загрузки сохранена: %s\n" "$workdir" >&2; fi' 0
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    archive="$workdir/source.tar.gz"
    download() {
        if [ "$downloader" = curl ]; then
            curl --fail --location --show-error --silent --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 180 --output "$2" "$1"
        else
            wget --https-only --timeout=30 --tries=2 -q -O "$2" "$1"
        fi
    }
    url="https://codeload.github.com/Lfyz-git/Stolas/tar.gz/$ref"
    case "$ref" in v[0-9]*)
        filename="stolas-$ref.tar.gz"
        base="https://github.com/Lfyz-git/Stolas/releases/download/$ref"
        url="$base/$filename"
        download "$base/SHA256SUMS" "$workdir/SHA256SUMS" || fail 'Не удалось получить контрольные суммы релиза.'
        release_hash=""
        while read -r hash name; do [ "$name" != "$filename" ] || release_hash=$hash; done < "$workdir/SHA256SUMS"
        [ -n "$release_hash" ] || fail 'В SHA256SUMS нет архива релиза.'
        [ -z "$expected" ] || [ "$expected" = "$release_hash" ] || fail 'Ожидаемая сумма не совпадает с релизом.'
        expected=$release_hash ;;
    esac
    printf 'Загрузка Stolas (%s)…\n' "$ref"
    download "$url" "$archive" || fail 'Не удалось загрузить архив. Проверьте сеть, REF и публичный доступ к репозиторию.'
    digest=$(sha256sum "$archive")
    digest=${digest%% *}
    if [ -n "$expected" ]; then
        [ "${#expected}" = 64 ] || fail 'Некорректный SHA-256.'
        [ "$digest" = "$expected" ] || fail 'Контрольная сумма архива не совпадает.'
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
    for file in install.sh tools/install.py tools/deploy.py agent/config.py compose.yaml config/example.json; do
        [ -f "$source/$file" ] && [ ! -L "$source/$file" ] || fail "В архиве отсутствует $file."
    done
    printf 'Запуск интерактивного мастера…\n'
    set -- --target "$target" --source-ref "$ref" --source-sha256 "$digest"
    [ -z "$action" ] || set -- "$@" --action "$action"
    [ "$configure_only" = false ] || set -- "$@" --configure-only
    sh "$source/install.sh" "$@"
    exit 0
}
main "$@"
