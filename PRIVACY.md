# Privacy

LARP Detector is a local research tool. It does not include a hosted account
system or an application telemetry service.

## Data stored on the Mac

The app can store:

- a dedicated LinkedIn browser profile under the user's Application Support
  directory
- local scan jobs, evidence, screenshots, and dossiers under `queue/`
- generated application bundles and helper tools under `.runtime/`
- local configuration in `.env`

These paths are excluded from Git. Anyone publishing a fork should still
inspect the exact staged file list because ignore rules do not remove data that
was committed previously.

## Network requests

A live scan can contact:

- LinkedIn to read the profile selected by the operator
- configured public search engines or search providers
- GitHub and other public registries used by evidence connectors
- public product websites for the bounded web-app check
- public favicon or image hosts used by evidence cards

When the Codex reviewer is enabled, the profile claims and gathered evidence
needed for judgment are processed through the Codex CLI and the user's ChatGPT
login. Optional API providers process the same task through the provider the
operator explicitly configures.

The browser companion sends only the active LinkedIn profile URL to the local
service on `127.0.0.1`. It does not request permission to read page content,
cookies, passwords, or general browser history.

## Screenshots

Screen capture is an optional fallback. If enabled and used, a captured image
can be passed to the configured reasoning provider to identify the target
profile. Avoid using this fallback when unrelated private information is
visible on screen.

## Deletion

Quit the app before deleting local state. Scan artifacts can be removed from
`queue/`. Generated bundles can be removed from `.runtime/`. The dedicated
LinkedIn session can be removed by deleting:

`~/Library/Application Support/LARP Detector/linkedin-profile`

Removing that directory signs the tool out of LinkedIn and requires the
one-time login flow again.
