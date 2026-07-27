#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}

if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  print "Missing .venv. Run ./scripts/setup_macos.sh first." >&2
  exit 1
fi

"$SCRIPT_DIR/prepare_macos_app.sh"

cd "$PROJECT_ROOT/overlay"
npm run build

# Launch through LaunchServices so macOS attributes privacy requests to the
# prepared LARP Detector app bundle, not to whichever terminal ran this script.
open -na "$PROJECT_ROOT/.runtime/LARP Detector.app" \
  --args "$PROJECT_ROOT/overlay"
