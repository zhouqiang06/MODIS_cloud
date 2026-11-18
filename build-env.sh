#!/usr/bin/env bash
set -euo pipefail

# Install into the MAAP base image's Conda environment. Do not create a uv/PyPI
# virtual environment: native wheels may introduce a second OpenSSL/libcrypto
# stack that is incompatible with the FIPS-enabled DPS worker.

basedir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

unset PROJ_LIB
unset PROJ_DATA

if ! command -v conda >/dev/null 2>&1; then
    echo "Error: the MAAP base image does not provide conda." >&2
    exit 1
fi

conda install -y \
    "numpy" \
    "rasterio>=1.4.3" \
    "pyhdf>=0.11.7" \
    "tqdm>=4.67.3" \
    "earthaccess>=0.14.0"

if [[ -n "${PYTHON_EXECUTABLE:-}" ]]; then
    runtime_python="${PYTHON_EXECUTABLE}"
elif command -v python3 >/dev/null 2>&1; then
    runtime_python="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    runtime_python="$(command -v python)"
else
    echo "Error: no Python interpreter is available after conda install." >&2
    exit 1
fi

if [[ ! -x "${runtime_python}" ]]; then
    echo "Error: Python interpreter is not executable: ${runtime_python}" >&2
    exit 1
fi

echo "DPS runtime Python: ${runtime_python}"

# Fail the image build early if imports or TLS are broken.
"${runtime_python}" -c \
    'import ssl; import earthaccess; import numpy; import rasterio; from pyhdf.SD import SD; print(ssl.OPENSSL_VERSION)'
"${runtime_python}" -c \
    'import urllib.request; response = urllib.request.urlopen("https://cmr.earthdata.nasa.gov/search/collections.json?page_size=1", timeout=30); response.read(1); print("TLS self-test passed")'

# Record the interpreter chosen during the build for troubleshooting.
printf '%s\n' "${runtime_python}" > "${basedir}/.dps-python-path"
