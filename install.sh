#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${PYTHON:-python3}

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12+ is required")
PY

if ! command -v pdftotext >/dev/null 2>&1; then
  printf '%s\n' 'pdftotext is required. Install poppler first.'
  printf '%s\n' 'macOS: brew install poppler'
  printf '%s\n' 'Ubuntu: sudo apt-get install poppler-utils'
  exit 1
fi

BIN_DIR=${TEAM_AI_BIN_DIR:-"$HOME/.local/bin"}
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/team-ai" <<EOF
#!/usr/bin/env sh
TEAM_AI_ROOT="$ROOT"
PYTHON_BIN="$PYTHON_BIN"
PYTHONPATH="\$TEAM_AI_ROOT/harness\${PYTHONPATH:+:\$PYTHONPATH}" exec "\$PYTHON_BIN" -m team_ai.cli "\$@"
EOF
chmod +x "$BIN_DIR/team-ai"

PYTHONPATH="$ROOT/harness" "$PYTHON_BIN" -m unittest discover -s "$ROOT/harness/tests"

printf '\nInstalled team-ai CLI.\n'
printf 'CLI wrapper: %s\n' "$BIN_DIR/team-ai"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf 'Add %s to PATH if team-ai is not found in a new shell.\n' "$BIN_DIR" ;;
esac
printf 'Create a project with: team-ai init project PROJECT_NAME\n'
