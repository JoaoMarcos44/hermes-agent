# Desktop local-gateway file authentication investigation (#104023)

## Result

The Download failure is reproducible when a token-authenticated local gateway
reaches the Electron file-save path with a missing connection token. The
backend requires its session token for `/api/fs/download` and
`/api/fs/read-data-url`; a credential-less request is rejected with HTTP 401.

The Preview part of the report is not caused by this token path in the current
Desktop code. Local preview resolves the artifact through the local filesystem
bridge. A ZIP is not a previewable document by design. An HTML preview failure
would need a separate artifact-path reproduction and is intentionally not
changed here.

An existing open PR, [#104044](https://github.com/NousResearch/hermes-agent/pull/104044),
implements the same Download fallback but uses the process environment as a
credential store and gates the fallback only on the URL hostname. This branch
retains its useful descriptor/pool recovery while closing that boundary: an
SSH-forwarded remote gateway also uses `127.0.0.1`, and child processes inherit
`process.env`.

## Evidence

### Runtime reproduction

A temporary `HERMES_HOME` and a real `python -m hermes_cli.main serve` process
were used with a non-secret test token and a temporary file. The same endpoint
returned:

- no session header: HTTP 401 (`Unauthorized`)
- `X-Hermes-Session-Token: null`: HTTP 401 (`Unauthorized`)
- the real session token: HTTP 200 with the file contents
- `Authorization: Bearer <test token>`: HTTP 200 with the file contents

The test token and temporary paths are not part of the repository or logs.
Node serializes a JavaScript `null` header value as the string `"null"`; it is
not an authenticated absence.

### Code path

Graphify located the relevant call chain:

- `downloadGatewayMediaFile()` → `saveGatewayFile()`
- `saveGatewayFile()` and `saveGatewayFileViaDataUrl()` → `gatedFileAuth()`
- `gatedFileAuth()` → `resolveGatedDownloadAuth()`
- local preview → `normalizeOrLocalPreviewTarget()` → `previewFileTarget()` →
  `resolveReadableFileForIpc()`

Before this change, `resolveGatedDownloadAuth('token', ..., null)` returned a
token auth decision whose token was `null`. Both file transports reused that
decision, so the compatibility fallback could not repair the authentication.
The local child already receives the adopted token at spawn and the resolved
primary connection contains it, but there was no recovery ladder for a
file-save descriptor that lost the token.

## Root cause

The file-save operation treats the resolved connection descriptor as the sole
source of the token. That descriptor is not guaranteed to retain the token in
every local routing path. The gateway is still session-gated, so the request
fails before the filesystem path is evaluated.

A hostname-only fallback is not safe. SSH port forwarding exposes a remote
backend through a local loopback URL. The backend identity therefore comes
from the connection descriptor's explicit `mode`, not from `localhost` or
`127.0.0.1` alone.

## Implementation

The existing auth chokepoint is extended rather than adding another download
transport:

1. Preserve a non-empty descriptor token exactly as before.
2. For an explicitly local, loopback connection, use the token of the pooled
   backend serving the same port when available.
3. For the primary local backend, read the live connection state and require an
   exact `baseUrl` match before using its token.
4. Keep OAuth bearer/cookie selection unchanged.
5. Apply the same decision to streaming downloads and the older data-URL
   fallback because both already call `gatedFileAuth()`.
6. Do not copy the dashboard token into `process.env`; this avoids inherited
   credential exposure and prevents a local token from being selected for an
   SSH-forwarded remote connection.

The local fallback now requires both `mode === 'local'` and an HTTP(S)
loopback URL. Remote descriptor tokens remain valid; only local fallback
candidates are denied to remote connections.

## Related work checked

- [#104044](https://github.com/NousResearch/hermes-agent/pull/104044) — open
  candidate for the same issue; not duplicated blindly because its
  environment-based, hostname-only fallback crosses the SSH loopback boundary.
- [#101094](https://github.com/NousResearch/hermes-agent/pull/101094) — open
  dashboard-token handoff through the Desktop ready-file startup path; relevant
  to token ownership, but it does not cover the file-save fallback.
- [#90546](https://github.com/NousResearch/hermes-agent/pull/90546) — merged
  OAuth bearer/cookie parity for gated file downloads; its auth decision is
  preserved.
- [#93547](https://github.com/NousResearch/hermes-agent/pull/93547) — merged
  connection scoping for SSH images, downloads, and media; its explicit remote
  routing is why loopback must not be treated as proof of local ownership.
- [#94833](https://github.com/NousResearch/hermes-agent/pull/94833) — open
  authenticated opening of remote media.
- [#101750](https://github.com/NousResearch/hermes-agent/pull/101750) — open
  local-artifact resolution while an SSH connection is active; related to the
  separate Preview/local-origin symptom.
- [#102667](https://github.com/NousResearch/hermes-agent/pull/102667) and
  [#102883](https://github.com/NousResearch/hermes-agent/pull/102883) — open
  remote media streaming and preview-owner routing.
- [#89013](https://github.com/NousResearch/hermes-agent/pull/89013) — closed in
  favor of #90546; confirms the older gated-download auth history.

## Security boundary

Local fallback credentials are never selected from a URL alone. They require an
explicit local connection and an exact local backend match. Remote connections
continue to use only their own descriptor credentials or OAuth session. The
main process does not mutate its environment with a live dashboard token, so
unrelated child processes do not inherit it.

No credentials, real user paths, or generated artifacts are stored in this
document.
