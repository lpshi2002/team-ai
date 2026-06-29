#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${PYTHON:-python3}

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12+ is required")
PY

"$PYTHON_BIN" -m pip install -e "$ROOT/harness"
PYTHONPATH="$ROOT/harness" "$PYTHON_BIN" -m unittest discover -s "$ROOT/harness/tests"

printf '\nInstalled team-ai CLI.\n'
printf 'Create a project with: team-ai init project PROJECT_NAME\n'
