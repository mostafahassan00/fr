#!/usr/bin/env bash
# Face Sorter launcher for macOS / Linux
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "First run: preparing, please wait a few minutes..."
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
exec .venv/bin/python face_sorter.py "$@"
