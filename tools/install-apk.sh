#!/bin/sh
set -eu
arch="$(apk --print-arch)"
mkdir /tmp/stolas-apks
# Generated TSVs are derived from the reviewed JSON lock, no live index is used.
while IFS=' ' read -r checksum url; do
    name="${url##*/}"
    printf 'Downloading locked APK: %s\n' "$url"
    if ! wget -q -O "/tmp/stolas-apks/$name" "$url"; then
        printf 'Locked APK unavailable: %s\nRun python3 tools/lock_apk.py --check before building.\n' "$url" >&2
        exit 1
    fi
    printf '%s  %s\n' "$checksum" "/tmp/stolas-apks/$name" | sha256sum -c -
done < "/tmp/apk-$arch.lock"
apk add --no-network /tmp/stolas-apks/*.apk
rm -rf /tmp/stolas-apks
rm /tmp/apk-*.lock
