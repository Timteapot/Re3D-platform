# Restricted deployment profile

This profile runs the real Re3D pipeline on one Windows workstation while
keeping the web application, API, PostgreSQL and Mailpit on loopback. It is for
local or otherwise physically restricted acceptance work, not for LAN or public
Internet exposure.

## Enforced boundary

- `APP_ENV=restricted` is distinct from development, test and production.
- API and public browser origins must use `localhost`, `127.0.0.0/8` or `::1`.
- trusted proxy networks and SMTP must remain on loopback.
- stable upload routes create only `real` jobs; development-only routes are absent.
- rate, transfer, task and user-storage limits must all be explicit.
- PostgreSQL must use database `re3d_platform_restricted` and the non-admin role
  `re3d_restricted_runtime` on loopback.
- task data must be outside the source checkout and meet the configured free-space
  floor. Successful input, runtime and artifact retention remains 30 days with
  scheduled cleanup in dry-run mode.

## Current implementation stage

Copy `.env.restricted.example` to `.env.restricted` and
`.env.worker.restricted.example` to `.env.worker.restricted`, but do not expect
readiness to pass until the next bootstrap step creates the dedicated database,
runtime role and data directory and applies migrations. Real passwords and JWT
secrets must stay only in the ignored copies.

Once those prerequisites exist, validate the complete single-host profile with:

```powershell
& .\deploy\restricted\check-restricted-readiness.ps1 -Component all
```

The command returns only non-secret identities, limits and dependency status.
The later start/stop orchestration must consume this profile and must not reuse
the development database or production service bundle.
