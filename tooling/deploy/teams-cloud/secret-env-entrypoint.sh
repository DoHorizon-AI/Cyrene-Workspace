#!/usr/bin/env sh
# ┌─────────────────────────────────────────────────────────────────────┐
# │  📄 secret-env-entrypoint.sh                                         │
# │  Role: Load named host secret files into one child process only.     │
# │                                                                      │
# │  脚本职责：仅为单个子进程读取主机密钥文件，不输出密钥内容。            │
# └─────────────────────────────────────────────────────────────────────┘

set -eu
set -f

if [ "$#" -eq 0 ]; then
    echo "A service command is required." >&2
    exit 64
fi

if [ -n "${CYRENE_SECRET_FILE_MAP:-}" ]; then
    old_ifs=$IFS
    IFS=,
    for mapping in $CYRENE_SECRET_FILE_MAP; do
        name=${mapping%%=*}
        file=${mapping#*=}
        case "$mapping" in
            *=*) ;;
            *) echo "Invalid secret-file mapping." >&2; exit 78 ;;
        esac
        case "$name" in
            ''|*[!A-Z0-9_]*) echo "Invalid secret environment name." >&2; exit 78 ;;
        esac
        case "$file" in
            /*) ;;
            *) echo "Secret files must use absolute paths." >&2; exit 78 ;;
        esac
        if [ -L "$file" ] || [ ! -f "$file" ] || [ ! -r "$file" ]; then
            echo "A required secret file is unavailable." >&2
            exit 78
        fi
        value=$(cat -- "$file")
        if [ -z "$value" ]; then
            echo "A required secret file is empty." >&2
            exit 78
        fi
        export "$name=$value"
        unset value
    done
    IFS=$old_ifs
    unset old_ifs mapping name file CYRENE_SECRET_FILE_MAP
fi

if [ -n "${CYRENE_INTERNAL_CA_FILE:-}" ]; then
    case "$CYRENE_INTERNAL_CA_FILE" in
        /*) ;;
        *) echo "Internal CA file must use an absolute path." >&2; exit 78 ;;
    esac
    if [ -L "$CYRENE_INTERNAL_CA_FILE" ] || [ ! -f "$CYRENE_INTERNAL_CA_FILE" ] || [ ! -r "$CYRENE_INTERNAL_CA_FILE" ]; then
        echo "Internal CA file is unavailable." >&2
        exit 78
    fi
    system_ca_file=/etc/ssl/certs/ca-certificates.crt
    if [ ! -r "$system_ca_file" ]; then
        echo "System CA bundle is unavailable." >&2
        exit 78
    fi
    old_umask=$(umask)
    umask 077
    ca_bundle=$(mktemp "${TMPDIR:-/tmp}/cyrene-system-ca.XXXXXX") || {
        echo "A temporary CA bundle could not be created." >&2
        exit 70
    }
    umask "$old_umask"
    if ! cat -- "$system_ca_file" "$CYRENE_INTERNAL_CA_FILE" >"$ca_bundle"; then
        rm -f -- "$ca_bundle"
        echo "The system and internal CA bundles could not be combined." >&2
        exit 78
    fi
    export SSL_CERT_FILE="$ca_bundle"
    export REQUESTS_CA_BUNDLE="$ca_bundle"
    export CURL_CA_BUNDLE="$ca_bundle"
    if [ -z "${NODE_EXTRA_CA_CERTS:-}" ]; then
        export NODE_EXTRA_CA_CERTS="$CYRENE_INTERNAL_CA_FILE"
    fi
    unset old_umask ca_bundle system_ca_file CYRENE_INTERNAL_CA_FILE
fi

exec "$@"
