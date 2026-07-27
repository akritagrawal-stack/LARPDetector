#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
ELECTRON_TEMPLATE_APP="$PROJECT_ROOT/overlay/node_modules/electron/dist/Electron.app"
LARP_APP="$PROJECT_ROOT/.runtime/LARP Detector.app"
INFO_PLIST="$LARP_APP/Contents/Info.plist"
PLIST_BUDDY=/usr/libexec/PlistBuddy
URL_HELPER_APP="$PROJECT_ROOT/.runtime/LARP URL Helper.app"
URL_HELPER_CONTENTS="$URL_HELPER_APP/Contents"
URL_HELPER_EXECUTABLE="$URL_HELPER_CONTENTS/MacOS/LARPURLHelper"
URL_HELPER_SOURCE="$SCRIPT_DIR/macos_url_helper.swift"
URL_HELPER_PLIST_SOURCE="$SCRIPT_DIR/macos_url_helper_Info.plist"
URL_HELPER_ENTITLEMENTS="$SCRIPT_DIR/macos_url_helper.entitlements"
URL_HELPER_BUILD_ID="$PROJECT_ROOT/.runtime/.macos-url-helper-build-id"
OVERLAY_ENTITLEMENTS="$SCRIPT_DIR/macos_overlay.entitlements"

if [[ ! -f "$ELECTRON_TEMPLATE_APP/Contents/Info.plist" ]]; then
  print "Electron is missing. Run npm install in $PROJECT_ROOT/overlay first." >&2
  exit 1
fi

if [[ ! -f "$INFO_PLIST" ]]; then
  mkdir -p "$PROJECT_ROOT/.runtime"
  ditto "$ELECTRON_TEMPLATE_APP" "$LARP_APP"
fi

changed=0

set_plist_string() {
  local key=$1
  local value=$2
  local current
  current=$("$PLIST_BUDDY" -c "Print :$key" "$INFO_PLIST" 2>/dev/null || true)
  if [[ "$current" == "$value" ]]; then
    return
  fi
  if "$PLIST_BUDDY" -c "Print :$key" "$INFO_PLIST" >/dev/null 2>&1; then
    "$PLIST_BUDDY" -c "Set :$key $value" "$INFO_PLIST"
  else
    "$PLIST_BUDDY" -c "Add :$key string $value" "$INFO_PLIST"
  fi
  changed=1
}

set_plist_string CFBundleIdentifier "com.larpdetector.overlay"
set_plist_string CFBundleName "LARP Detector"
set_plist_string CFBundleDisplayName "LARP Detector"
set_plist_string NSAppleEventsUsageDescription \
  "LARP Detector reads the active browser tab URL when you request a scan."
set_plist_string NSScreenCaptureUsageDescription \
  "LARP Detector captures the active display as a fallback when a page URL cannot be read."

overlay_entitlements=$(codesign -d --entitlements - "$LARP_APP" 2>/dev/null || true)
if [[ "$overlay_entitlements" != *"com.apple.security.automation.apple-events"* ]]; then
  changed=1
fi

if (( changed )) || ! codesign --verify --deep --strict "$LARP_APP" >/dev/null 2>&1; then
  codesign --force --deep --sign - \
    --identifier "com.larpdetector.overlay" \
    --entitlements "$OVERLAY_ENTITLEMENTS" \
    "$LARP_APP"
fi

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$LARP_APP"

url_helper_hash=$(
  shasum -a 256 \
    "$URL_HELPER_SOURCE" \
    "$URL_HELPER_PLIST_SOURCE" \
    "$URL_HELPER_ENTITLEMENTS" |
    shasum -a 256 |
    awk '{print $1}'
)
current_url_helper_hash=""
if [[ -f "$URL_HELPER_BUILD_ID" ]]; then
  current_url_helper_hash=$(<"$URL_HELPER_BUILD_ID")
fi

if [[ "$current_url_helper_hash" != "$url_helper_hash" ]] ||
   [[ ! -x "$URL_HELPER_EXECUTABLE" ]] ||
   ! codesign --verify --deep --strict "$URL_HELPER_APP" >/dev/null 2>&1; then
  mkdir -p "$URL_HELPER_CONTENTS/MacOS"
  cp "$URL_HELPER_PLIST_SOURCE" "$URL_HELPER_CONTENTS/Info.plist"
  xcrun swiftc -O "$URL_HELPER_SOURCE" -o "$URL_HELPER_EXECUTABLE" \
    -framework AppKit -framework Carbon
  codesign --force --sign - --options runtime \
    --identifier "com.larpdetector.urlhelper" \
    --entitlements "$URL_HELPER_ENTITLEMENTS" "$URL_HELPER_APP"
  print -r -- "$url_helper_hash" > "$URL_HELPER_BUILD_ID"
fi

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$URL_HELPER_APP"
