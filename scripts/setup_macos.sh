#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}

if [[ "$(uname -s)" != "Darwin" ]]; then
  print "This setup script supports macOS only." >&2
  exit 1
fi

for command_name in python3 node npm xcrun; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    print "Missing required command: $command_name" >&2
    print "See the prerequisites section in README.md." >&2
    exit 1
  fi
done

if ! xcrun --find swiftc >/dev/null 2>&1; then
  print "Swift compiler not found. Install Xcode Command Line Tools:" >&2
  print "  xcode-select --install" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
if [[ ! -f .env ]]; then
  cp .env.example .env
  print "Created .env from the free, no-key defaults in .env.example."
fi
chmod 600 .env

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q

cd "$PROJECT_ROOT/overlay"
npm ci

ELECTRON_INSTALLER="$PROJECT_ROOT/overlay/node_modules/.bin/install-electron"
ELECTRON_BINARY="$PROJECT_ROOT/overlay/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron"
if [[ ! -x "$ELECTRON_BINARY" && -x "$ELECTRON_INSTALLER" ]]; then
  "$ELECTRON_INSTALLER"
fi
if [[ ! -x "$ELECTRON_BINARY" ]]; then
  print "Electron's macOS application binary was not installed." >&2
  print "Check your network connection, then rerun ./scripts/setup_macos.sh." >&2
  exit 1
fi

npm test
npm run build

print "macOS setup complete. Run: ./scripts/run_macos.sh"
