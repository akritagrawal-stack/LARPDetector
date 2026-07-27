function summonShortcutForPlatform(platform) {
  // Cmd+Space belongs to Spotlight on macOS. Electron's
  // CommandOrControl+Space therefore cannot be registered there.
  return platform === 'darwin' ? 'Control+Space' : 'CommandOrControl+Space';
}

module.exports = { summonShortcutForPlatform };
