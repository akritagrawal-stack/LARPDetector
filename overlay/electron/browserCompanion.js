const fs = require('fs');
const path = require('path');

function samePath(left, right) {
  if (!left || !right) return false;
  try {
    return path.resolve(String(left)) === path.resolve(String(right));
  } catch {
    return false;
  }
}

function chromiumSettingsHaveCompanion(settings, extensionDir) {
  for (const item of Object.values(settings || {})) {
    if (samePath(item && item.path, extensionDir)) return true;
  }
  return false;
}

function chromiumProfileDirs(rootDir) {
  try {
    return fs.readdirSync(rootDir, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .filter((entry) => entry.name === 'Default' || entry.name.startsWith('Profile '))
      .map((entry) => path.join(rootDir, entry.name));
  } catch {
    return [];
  }
}

function isBrowserCompanionInstalled(homeDir, extensionDir) {
  const support = path.join(homeDir, 'Library', 'Application Support');
  const roots = [
    path.join(support, 'Google', 'Chrome'),
    path.join(support, 'Comet'),
    path.join(support, 'BraveSoftware', 'Brave-Browser'),
    path.join(support, 'Microsoft Edge'),
    path.join(support, 'Arc', 'User Data')
  ];
  for (const root of roots) {
    for (const profileDir of chromiumProfileDirs(root)) {
      const preferencesPath = path.join(profileDir, 'Secure Preferences');
      try {
        const preferences = JSON.parse(fs.readFileSync(preferencesPath, 'utf8'));
        if (
          chromiumSettingsHaveCompanion(
            preferences.extensions && preferences.extensions.settings,
            extensionDir
          )
        ) {
          return true;
        }
      } catch {
        // A missing, locked, or malformed profile is not an installation.
      }
    }
  }
  return false;
}

module.exports = {
  chromiumSettingsHaveCompanion,
  isBrowserCompanionInstalled
};
