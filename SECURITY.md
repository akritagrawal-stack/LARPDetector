# Security

## Reporting a vulnerability

Please open a GitHub security advisory for vulnerabilities that could expose
browser state, LinkedIn session data, local files, credentials, or private scan
results. Do not include secrets, cookies, access tokens, or private dossier
contents in a public issue.

## Local data boundary

The app is designed to keep credentials and scan artifacts local:

- `.env`, `queue/`, `.runtime/`, browser profiles, cookies, and storage-state
  files are excluded from Git.
- The browser companion shares only the active LinkedIn profile URL with the
  local service.
- The runtime web-app check does not enter credentials or submit forms.
- GitHub CLI authentication is read from the operating system keychain. Tokens
  should not be copied into the repository.

Before publishing a fork, review the exact staged file set and run a secret
scanner. A clean working directory alone is not proof that generated dossiers
or credentials are safe to publish.
