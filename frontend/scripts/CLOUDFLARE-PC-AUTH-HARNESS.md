# Isolated desktop Google verification

After the staging owner confirms the matching web/auth build is ready, run from
the repository root:

```powershell
node frontend/scripts/cloudflare-pc-auth-harness.mjs --run-stage
```

The script transpiles and directly invokes the current `CloudSync`,
`cloudProvider` and `oauth-loopback` sources without starting Electron. It prints
one authorization URL. Open only that URL in the authorized browser session;
the script never controls a browser. Google returns through the staging HTTPS
callback and then the application's exact `127.0.0.1:43873/cf-auth/callback`
listener, with its real state and S256 validation. A fresh attempt times out
after five minutes. Do not run the real MPI app's login simultaneously on that port.

The default permitted server origin is
`https://mpi-migration-stage.ketchup7124.workers.dev`; there is no arbitrary origin override.
Run with network access to that origin: a restricted local Node sandbox can reject
`fetch` with `EACCES` before the server receives the exchange. This is a transport
failure, not evidence of an OAuth or PKCE failure; start a new browser attempt
after granting the necessary stage network access.
The request allowlist covers native auth, entitlement and delegated auth only.
There is no capture, session upload, payment or external identity-provider request.
The ordinary auth exchange, short-lived delegation and logout do mutate staging
authentication state; Google may create an account under the server's current policy.

After the release coordinator explicitly confirms production cutover is live,
the same flow can target the fixed `https://mpinspect.com` origin:

```powershell
node frontend/scripts/cloudflare-pc-auth-harness.mjs --run-production --expected-accounts <local-json-file>
```

Production requires a local JSON file containing `accounts`, an array of
`{userId, entitlement}` records prepared from a read-only production D1 query.
`entitlement` must include `plan` and may include `remaining_seconds`,
`storage_remaining`, `storage_unlimited`, `cloud_storage` and `pooled`.
Use an existing Google account from that allowlist. A missing or unexpected UUID,
or any mismatched entitlement value, fails verification and triggers logout.
Only match booleans and checked field names are emitted; UUIDs and emails are not
printed. This mode creates temporary production authentication/delegation state
and revokes it at the end. Do not start it while maintenance/cutover is pending.

Actual application methods perform Google login, online session verification,
entitlement parsing, Companion delegation and logout. The harness also checks
that the delegated bearer has the same owner and `android_companion` kind, then
requires HTTP 401 for desktop access, delegated access and refresh after logout.
No native list GET exists, so list retrieval is explicitly untested.

A newly created OS temporary directory replaces the Electron profile. The secure
storage adapter writes a constant public marker instead of any credential or
encrypted credential, and cleanup removes only that verified temporary directory.
Tokens and identity stay in process memory and are never printed or written.
The script emits only the authorize URL, allowed request paths and HTTP status,
allowlisted server error enums, token-format booleans/remaining lifetime, and fixed
phase results; request or error bodies are never logged. Local DPAPI/safeStorage, restore across restarts, renderer
UI and actual USB token delivery are **not** verified by this harness.

Run the network-free server simulations (local loopback HTTP is used) with:

```powershell
node --test frontend/scripts/cloudflare-pc-auth-harness.test.mjs
```

These simulations reuse the real application sources and cover state rejection,
S256 binding, correct callback exchange, native/delegated identity, revocation,
network allowlisting, partial-failure cleanup and absence of secrets in output.
