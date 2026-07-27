# LARP Detector browser companion

This extension shares only the active `linkedin.com/in/...` URL with the local
LARP Detector service. It does not read page content, cookies, passwords, or
browsing history.

Install in Chrome, Comet, Brave, Edge, Arc, or another Chromium browser:

1. Open `chrome://extensions`.
2. Turn on Developer mode.
3. Choose Load unpacked.
4. Select this `browser-extension` folder.

Keep LARP Detector running. Opening a LinkedIn profile makes Browser companion
show Connected in the app's Settings panel. No macOS Automation permission is
required.

Safari does not use this unpacked extension. LARP Detector reads Safari's
active URL through macOS Automation after the user approves the system prompt,
or accepts a pasted profile URL. Open `INSTALL.html` for the Chromium setup
steps.
