#!/usr/bin/env bash
set -euo pipefail

# DPS entry point for one day of MOD35_L2 processing.

basedir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

usage() {
    cat >&2 <<EOF
Usage:
  $0 YYYY-MM-DD
  $0 YYYY-MM-DD --earthdata_username USER --earthdata_password_file PATH

Raw password command-line values are intentionally unsupported.
EOF
}

if [[ $# -lt 1 ]]; then
    echo "Error: processing date is required." >&2
    usage
    exit 2
fi

proc_date="$1"
shift

if [[ ! "${proc_date}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "Error: processing date must use YYYY-MM-DD format." >&2
    usage
    exit 2
fi

earthdata_username=""
earthdata_password_file=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --earthdata_username)
            if [[ $# -lt 2 ]]; then
                echo "Error: --earthdata_username requires a value." >&2
                exit 2
            fi
            if [[ -z "$2" ]]; then
                echo "Error: --earthdata_username cannot be empty." >&2
                exit 2
            fi
            earthdata_username="$2"
            shift 2
            ;;
        --earthdata_password_file)
            if [[ $# -lt 2 ]]; then
                echo "Error: --earthdata_password_file requires a path." >&2
                exit 2
            fi
            if [[ -z "$2" ]]; then
                echo "Error: --earthdata_password_file cannot be empty." >&2
                exit 2
            fi
            earthdata_password_file="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --earthdata_password)
            echo "Error: raw password arguments are unsafe and unsupported." >&2
            echo "Use --earthdata_password_file with a chmod 600 file." >&2
            exit 2
            ;;
        *)
            echo "Error: unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -n "${earthdata_username}" || -n "${earthdata_password_file}" ]]; then
    if [[ -z "${earthdata_username}" || -z "${earthdata_password_file}" ]]; then
        echo "Error: --earthdata_username and --earthdata_password_file must be used together." >&2
        exit 2
    fi
    if [[ ! -f "${earthdata_password_file}" ]]; then
        echo "Error: password file does not exist: ${earthdata_password_file}" >&2
        exit 2
    fi
    export EARTHACCESS_LOGIN_STRATEGY="environment"
fi

# DPS persists files written beneath the relative output directory.
output_dir="${PWD}/output"
mkdir -p "${output_dir}"

# Avoid inheriting host PROJ paths that conflict with Rasterio/GDAL in the
# uv-managed Python environment.
unset PROJ_LIB
unset PROJ_DATA

# Interactive Earthaccess authentication launches a browser and is unsuitable
# for a headless DPS container. Prefer safe parameters, injected secrets, then
# a mounted netrc.
if [[ -z "${earthdata_username}" && -z "${EARTHACCESS_LOGIN_STRATEGY:-}" ]]; then
    if [[ -n "${EARTHDATA_USERNAME:-}" && -n "${EARTHDATA_PASSWORD:-}" ]]; then
        export EARTHACCESS_LOGIN_STRATEGY="environment"
    elif [[ -f "${HOME}/.netrc" ]]; then
        export EARTHACCESS_LOGIN_STRATEGY="netrc"
    else
        echo "Error: Earthdata credentials were not provided." >&2
        echo "Set EARTHDATA_USERNAME and EARTHDATA_PASSWORD, or mount ${HOME}/.netrc." >&2
        exit 3
    fi
fi

case "${EARTHACCESS_LOGIN_STRATEGY}" in
    environment)
        if [[ -z "${earthdata_username}" ]]; then
            : "${EARTHDATA_USERNAME:?EARTHDATA_USERNAME is required for environment login}"
            : "${EARTHDATA_PASSWORD:?EARTHDATA_PASSWORD is required for environment login}"
        fi
        ;;
    netrc)
        if [[ ! -f "${HOME}/.netrc" ]]; then
            echo "Error: ${HOME}/.netrc does not exist." >&2
            exit 3
        fi
        ;;
    interactive)
        echo "Error: interactive Earthdata login is not supported in headless DPS." >&2
        exit 3
        ;;
    *)
        echo "Error: unsupported EARTHACCESS_LOGIN_STRATEGY=${EARTHACCESS_LOGIN_STRATEGY}" >&2
        exit 3
        ;;
esac

python_script="${MOD35_SCRIPT:-${basedir}/mod35_daily_tif.py}"
if [[ ! -f "${python_script}" && -f "${basedir}/mod35_daily.py" ]]; then
    python_script="${basedir}/mod35_daily.py"
fi
if [[ ! -f "${python_script}" ]]; then
    echo "Error: MOD35 Python script not found: ${python_script}" >&2
    exit 4
fi

credential_args=()
if [[ -n "${earthdata_username}" ]]; then
    credential_args+=(
        --earthdata_username "${earthdata_username}"
        --earthdata_password_file "${earthdata_password_file}"
    )
fi

exec env UV_PROJECT="${basedir}" \
    uv run --project "${basedir}" --no-dev \
    "${python_script}" \
    --proc_date "${proc_date}" \
    --output_dir "${output_dir}" \
    "${credential_args[@]}"
