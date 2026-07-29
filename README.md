# LARP Detector for macOS

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

LARP Detector is a local macOS app that turns a LinkedIn profile or company URL
into an evidence-backed dossier. It checks public career claims, product
reality, public GitHub work, role attribution, and web-app behavior, then shows
the evidence and a blunt credibility assessment in a floating Electron
overlay.

The app is built for macOS. The Python engine remains portable, but the
prepared app bundle, permission flow, browser-link helper, and global shortcut
documented here are macOS-specific.

## Important boundary

This is an investigative aid, not a truth machine. It distinguishes:

- `CONFIRMED`: qualifying evidence supports the claim.
- `UNVERIFIED`: the search did not establish the claim either way.
- `DISPROVEN`: qualifying evidence actively contradicts the claim.

A missing result is not automatically a lie. Search outages are not treated as
completed searches. Personal sites and self-authored LinkedIn posts are labeled
subject-controlled. People-data aggregators are labeled republications, not
independent reporting. A live product proves that the product exists, not that
the named person founded or built it.

Every completed scan reports three numbers:

- **Overall LARP**: the headline score.
- **Founder/person**: claim credibility from role, identity, education, and
  technical-substance evidence.
- **Company**: product realness, liveness, public footprint, App Store or web
  app evidence, and buildability.

On a person scan the dossier engine creates a separate assessment for every
named employment company. Current and founder-linked companies can affect the
overall score. Historical employers are still checked and shown, but remain
context-only so a former employee does not inherit an old employer's risk.
When both main components are available, overall is 60% person and 40% company.
The code preserves the accusation boundary: two suspicion-only components
cannot combine into the top LARP band, and a proven contradiction cannot be
diluted out of that band.

Use the app only for lawful review of public professional claims. Do not use a
score as the sole basis for employment, housing, credit, education, or another
high-impact decision.

## Cost

The default local setup requires:

- no Docker
- no paid search API
- no separate model API key

Free search uses DDGS. It is useful for testing but public search engines can
throttle it. The Electron app can use the Codex CLI bundled with the ChatGPT
desktop app for reasoning through your existing ChatGPT login. This is not an
OpenAI API integration and does not require an `OPENAI_API_KEY`.

Optional Tavily, Exa, Brave, Gemini, PitchBook, and SearXNG integrations remain
off unless you configure them.

## macOS prerequisites

Recommended:

- macOS 13 or newer
- Apple Silicon or Intel Mac
- Python 3.11 or 3.12
- Node.js 22.12 or newer
- Xcode Command Line Tools
- Google Chrome for the one-time dedicated LinkedIn login
- Chrome, Brave, Edge, Arc, or Comet for the optional browser companion
- ChatGPT desktop, logged in, for the no-separate-bill Codex reviewer

Check the tools:

```bash
python3 --version
node --version
npm --version
xcrun swiftc --version
```

If Xcode Command Line Tools are missing:

```bash
xcode-select --install
```

If Python or Node is missing and you use Homebrew:

```bash
brew install python@3.12 node
```

Homebrew is convenient, not required.

## Install on macOS

```bash
git clone https://github.com/akritagrawal-stack/LARPDetector.git
cd LARPDetector
./scripts/setup_macos.sh
```

The setup script:

1. verifies that it is running on macOS
2. checks Python, Node, npm, xcrun, and the Swift compiler
3. creates `.env` from the free defaults when `.env` does not exist
4. creates a project-local `.venv`
5. installs Python dependencies and Playwright Chromium
6. runs the Python test suite
7. installs exact Electron dependencies with `npm ci`
8. verifies that Electron's macOS application binary was downloaded
9. runs Electron tests and builds the renderer

No global Python package is installed.

Start the app from the repository directory:

```bash
./scripts/run_macos.sh
```

The script prepares and ad-hoc signs local `LARP Detector.app` and
`LARP URL Helper.app` bundles under `.runtime/`, registers them with macOS, and
opens the overlay through LaunchServices. Generated app bundles are local and
are never committed.

## First-run setup

Open the overlay Settings panel and complete the checks that apply to you.

### 1. LinkedIn login

Choose the LinkedIn login action. The app opens a dedicated local browser
profile. Sign in to LinkedIn there once. That session stays in your local
Application Support directory and is not stored in this repository.

### 2. Browser companion, recommended

For Chrome, Brave, Edge, Arc, or Comet:

1. In the app, open Settings and choose **Install extension**.
2. Open the browser's extensions page.
3. Enable Developer mode.
4. Choose **Load unpacked**.
5. Select this repository's `browser-extension` directory.
6. Return to the app. Settings should show the companion as connected after
   the browser reports the active LinkedIn tab.

The companion sends only the active `linkedin.com/in/...` URL to the local
service. It does not read page content, cookies, passwords, or browser history.

Safari does not use this unpacked Chromium extension. Safari works through the
macOS Automation URL helper or by pasting a complete LinkedIn URL.

### 3. macOS Automation permission

macOS does not list an app under **Privacy & Security > Automation** until the
app has requested access at least once.

To trigger the request:

1. Open the browser you want to use.
2. Put a LinkedIn profile in the active tab.
3. Open LARP Detector.
4. press `Control+Space` or start a scan from the overlay
5. approve the macOS prompt

If you declined the prompt, open:

**System Settings > Privacy & Security > Automation > LARP Detector**

Enable the browser you want LARP Detector to read. Permission is browser
specific. If Chrome is enabled, that does not automatically enable Safari,
Brave, Arc, Edge, or Comet.

The browser companion avoids this permission for supported Chromium browsers.

### 4. Screen Recording, optional

Screen Recording is only a fallback when the browser URL and clipboard routes
both fail. The runtime product check uses its own headless Chromium and does
not require Screen Recording.

If wanted, enable:

**System Settings > Privacy & Security > Screen & System Audio Recording**

Then restart only LARP Detector. You do not need to restart Codex or ChatGPT.

### 5. GitHub authentication, optional but recommended

Public GitHub inspection works without authentication at a small shared rate
limit. For more reliable technical scans:

```bash
brew install gh
gh auth login
gh auth status
```

The app reuses GitHub CLI authentication from macOS Keychain. Do not paste a
GitHub token into the repository. `GH_TOKEN` or `GITHUB_TOKEN` is intended only
for CI or a headless environment and must remain outside Git.

## Use the app

1. Open a LinkedIn profile in your browser.
2. Press `Control+Space`.
3. Review the detected URL in the overlay.
4. Start the scan.
5. Watch evidence and coverage status stream into the panel.
6. Expand the final dossier to inspect source URLs and reasoning.

You can also paste a complete `https://www.linkedin.com/in/...` URL or a company
website directly into the overlay.

`Command+Space` is not the default because macOS reserves it for Spotlight.
The app uses `Control+Space`. If that shortcut is already assigned to an input
source, change or disable the conflicting shortcut under:

**System Settings > Keyboard > Keyboard Shortcuts > Input Sources**

Then restart LARP Detector.

## Free search without Docker

`scripts/setup_macos.sh` copies `.env.example` to `.env`. The default is:

```dotenv
DDGS_ENABLED=1
DDGS_BACKEND=bing,yahoo,duckduckgo
```

This costs nothing and needs no account. Public engines may return CAPTCHAs or
rate limits, so the engine records the difference between a completed empty
search and an unavailable search.

SearXNG is optional. It is the better choice for sustained or shared use
because you control the search frontend, but it is not required for local
testing. If you later run one:

```dotenv
SEARXNG_URL=http://127.0.0.1:8080
```

Tavily, Exa, and Brave are optional alternatives:

```dotenv
TAVILY_API_KEY=
EXA_API_KEY=
BRAVE_API_KEY=
```

Free allowances and provider terms can change. The app does not create
accounts, add billing, or enable these services without a key you provide.

## How role verification works

Employment searches preserve real title phrases. A compound title such as
`Head of Growth & Creative Director` is searched as meaningful facets rather
than the distorted phrase `Head Growth Creative Director`.

The evidence layer records:

- whether the result binds the named person, organization, and role
- whether it is subject-controlled, first-party organization material, an
  independent third party, a platform artifact, or a republication
- whether the result is substantive role evidence or association only
- whether the search completed, returned nothing, or was unavailable

High-public roles such as founder, CEO, CTO, head, and director receive a
targeted role follow-up when broad retrieval found only personal,
republication, or association evidence. Code also downgrades an unsupported
`CONFIRMED` or `DISPROVEN` label before a result reaches the UI.

GitHub inspection verifies public artifact structure, sampled authorship,
tests, CI, manifests, frameworks, build commands, and deployment hints. It
does not prove private work, originality, runtime correctness, security, or a
job title.

The web-app check opens public product pages in bundled headless Chromium. It
can identify a real interaction or authentication surface, but it does not
enter credentials or submit forms.

## CLI

Offline demo:

```bash
.venv/bin/python -m detective --demo
```

Real profile, with a live LinkedIn fetch:

```bash
.venv/bin/python -m detective \
  https://www.linkedin.com/in/someone/ \
  --live
```

The CLI defaults to `ManualProvider`, which writes operator jobs under
`queue/`. The Electron app selects the Codex reviewer when the bundled Codex
CLI is available. An optional separately metered Gemini path requires both an
explicit provider selection and `GEMINI_API_KEY`.

## Tests

```bash
.venv/bin/python -m pytest -q
cd overlay
npm test
npm run build
```

All unit and regression tests are offline. Live verification is a separate,
explicit operator action because public search results and websites change.

## Architecture

```text
detective/
  extract_linkedin.py  LinkedIn extraction and parser
  dossier.py           aggregate, follow-up, and mismatch stages
  verify.py            search queries, provenance, and evidence routing
  sources/             GitHub, registries, product, web-app, and other sources
  llm.py               operator contracts, safety gates, and scoring
  service.py           local FastAPI and WebSocket service
overlay/
  electron/            macOS app shell, permissions, shortcut, browser bridge
  src/                 glass overlay UI
browser-extension/     active LinkedIn URL companion for Chromium browsers
scripts/               macOS setup, signing, login, and launch helpers
tests/                 offline unit and regression suite
```

See [Architecture](docs/ARCHITECTURE.md) and
[Operator protocol](docs/OPERATOR_PROTOCOL.md) for more detail.

## Troubleshooting

### `./scripts/run_macos.sh: no such file or directory`

You are not in the repository directory:

```bash
cd /path/to/LARPDetector
./scripts/run_macos.sh
```

### The app does not appear

```bash
./scripts/run_macos.sh
pgrep -fal "LARP Detector|service_run.py"
```

If setup was incomplete, rerun:

```bash
./scripts/setup_macos.sh
```

### `Control+Space` does nothing

Check for an Input Sources shortcut conflict, restart LARP Detector, and use
the URL field once to confirm the overlay itself is healthy.

### Browser URL permission keeps returning

Use the browser companion for Chrome, Brave, Edge, Arc, or Comet. For Safari,
trigger the Automation request with Safari open and a LinkedIn profile active,
then enable Safari under LARP Detector in System Settings.

### Search returns little evidence

Open the dossier coverage details. `unavailable` means the search channel was
blocked or throttled and must not be interpreted as a completed absence. Wait
for the cooldown, switch DDGS backends, or configure your own SearXNG.

### GitHub still shows disconnected

Run:

```bash
gh auth status
```

Then restart LARP Detector so the local service refreshes its authentication
status. The app reads Keychain through GitHub CLI and never copies the token
into `.env`.

## Privacy and repository hygiene

The repository excludes:

- `.env`
- operator queue jobs and dossiers
- Playwright and LinkedIn browser profiles
- cookies and storage-state files
- generated app bundles
- `node_modules` and virtual environments
- private target lists and local research output

Before publishing a fork, inspect the exact staged files and run a secret
scanner. See [SECURITY.md](SECURITY.md) and [PRIVACY.md](PRIVACY.md).

## License and reuse

LARP Detector is open-source software licensed under the
[MIT License](LICENSE). You may use, copy, modify, merge, publish, distribute,
sublicense, and sell copies, provided that the copyright and permission notice
remain included in copies or substantial portions of the software.

Bundled fonts and other third-party components retain their original licenses.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution details.
