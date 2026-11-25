# Desktop cloud connection

The Electron app uses Cloudflare authentication and data at `https://mpinspect.com`.
These defaults are compiled into main; launching the packaged app requires no
process variables. An isolated preview can select a different HTTPS origin:

```powershell
$env:MPI_CF_API_ORIGIN = 'https://your-isolated-preview.example'
npm run electron:dev
```

The origin must be canonical, with no path, query, fragment or credentials.
Invalid explicit configuration fails instead of falling back to production.
`MPI_BACKEND`, when supplied, must be `cloudflare`; no other backend is supported.
No renderer IPC or Companion token can choose a backend or URL. Setting process
variables while building does not change the package's compiled defaults. The
Mobile app connections must use the same origin. The Android app source is managed separately.

Google is the only interactive login method. There is no MPI password, signup or
reset form. The first successful Google login creates an account when enabled by
the server.

Authentication uses `/cf-auth/google/start`, `/cf-auth/exchange`,
`/cf-auth/session`, `/cf-auth/refresh`, and `/cf-auth/logout`. Native Google login
uses an external browser, a per-attempt S256 verifier and random state, and the
exact loopback callback `http://127.0.0.1:43873/cf-auth/callback`. Configure this
callback in the auth server's `CF_AUTH_NATIVE_REDIRECTS`. Google itself returns
to the HTTPS server callback configured by the auth service. Tokens are exchanged
only in main, encrypted with Electron safeStorage, and absent from renderer status.

The desktop entitlement/capture/session DTOs are unchanged. The Cloudflare API
origin serves `/api/desktop/entitlement`, `/api/desktop/captures` and
`/api/desktop/sessions`. PC-to-mobile sync calls `/cf-auth/companion-token` for a
separate short-lived `android_companion` session. It caches that lease until sixty
seconds remain; the Python bridge's exact three-field token DTO is unchanged.
Account replacement, logout, parent revocation and the Companion heartbeat
continue to invalidate the lease.

Authentication, capture completions and queues remain under
`cloudflare-<origin SHA256 prefix>` inside the existing user data directory.
New files use `cloud-auth.json`, `cloud-outbox` and `capture-finishes.json`.
On the first launch with this schema, the app can copy a single recognized older
auth/outbox pair in that same origin's directory after validating encrypted token
and owner-bound queue formats. It preserves originals, rejects ambiguous sources,
never imports another origin or top-level credentials, and records a durable
schema marker so logout cannot resurrect an older login. Invalid local data stays
on disk for recovery and is reported as a storage error.

Verification covers Google state/PKCE rejection and replay, encrypted persistence,
schema upgrades, unsupported backend rejection, refresh identity checks, durable
uploads, capture quota lifecycles and delegated-token account isolation. Run
`npm test -- --configLoader runner` and
`node --test scripts/cloudflare-pc-auth-harness.test.mjs`. Real Google consent and
USB transfer still require a configured server and matching APK.
