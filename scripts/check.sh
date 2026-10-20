#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
check_python="${PYTHON:-python}"
if [ -x .venv/bin/python ]; then check_python=.venv/bin/python; fi
"$check_python" -m ruff check .
"$check_python" -m ruff format --check .
"$check_python" -m mypy
"$check_python" scripts/check_js.py
"$check_python" -m pytest -q
