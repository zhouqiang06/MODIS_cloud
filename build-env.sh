#!/usr/bin/env bash
set -euo pipefail

# Build the runtime environment with the MAAP base image's existing Python.
# Preventing uv from downloading a managed Python avoids crypto/FIPS library
# mismatches between the build and DPS worker environments.

basedir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

unset PROJ_LIB
unset PROJ_DATA

if [[ -n "${PYTHON_EXECUTABLE:-}" ]]; then
    system_python="${PYTHON_EXECUTABLE}"
elif command -v python3 >/dev/null 2>&1; then
    system_python="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    system_python="$(command -v python)"
else
    echo "Error: the MAAP base image does not provide Python." >&2
    exit 1
fi

if [[ ! -x "${system_python}" ]]; then
    echo "Error: base Python is not executable: ${system_python}" >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    conda install -y curl
fi

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

export UV_PYTHON_DOWNLOADS=never

echo "Building with base Python: ${system_python}"
uv sync \
    --project "${basedir}" \
    --python "${system_python}" \
    --no-dev

venv_python="${basedir}/.venv/bin/python"
if [[ ! -x "${venv_python}" ]]; then
    echo "Error: uv did not create ${venv_python}." >&2
    exit 1
fi

# Fail the image build early if native libraries or imports are inconsistent.
"${venv_python}" -c \
    'import ssl; import earthaccess; import numpy; import rasterio; from pyhdf.SD import SD; print(ssl.OPENSSL_VERSION)'

# Exercise an actual TLS handshake during the image build instead of finding a
# crypto/FIPS mismatch only after the DPS job starts.
"${venv_python}" -c \
    'import urllib.request; response = urllib.request.urlopen("https://cmr.earthdata.nasa.gov/search/collections.json?page_size=1", timeout=30); response.read(1); print("TLS self-test passed")'
