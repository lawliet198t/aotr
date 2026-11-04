#!/usr/bin/env bash
# Build fast_capture.so — run once from the project root
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-$(which .venv/bin/python3 2>/dev/null || which python3)}"
NUMPY_INC=$($PYTHON -c "import numpy; print(numpy.get_include())")
PY_INC=$(python3-config --includes)
PY_LDFLAGS=$(python3-config --ldflags 2>/dev/null || echo "")

echo "[+] Building fast_capture.so ..."
gcc -O3 -march=native -ffast-math -fPIC -shared \
    $PY_INC \
    -I"$NUMPY_INC" \
    fast_capture.c \
    -o fast_capture.so \
    $(pkg-config --libs x11 xext xi) \
    $PY_LDFLAGS \
    -lpthread

echo "[+] Done: fast_capture.so"
