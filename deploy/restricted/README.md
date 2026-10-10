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

## Initialization order

First validate the database contract without touching the installed PostgreSQL:

```powershell
& .\deploy\postgres\run-restricted-database-acceptance.ps1
```

Then initialize the installed local PostgreSQL. Choose different passwords of
at least 16 characters for the migration and runtime roles:

```powershell
& .\deploy\postgres\bootstrap-restricted.ps1
& .\deploy\postgres\migrate-restricted.ps1
```

Finally create the ignored API/Worker environment files and external data root.
Enter the same runtime password used above:

```powershell
& .\deploy\restricted\initialize-restricted-env.ps1
```

Every script accepts `SecureString` parameters for automation but never prints
or persists the administrator or migration passwords. Only the runtime password
and a generated JWT secret are saved in the ignored local environment files.
The initialization scripts refuse to overwrite either environment file. Both
files receive protected Windows ACLs limited to the creating user,
Administrators and SYSTEM. Existing files can be hardened and verified with:

```powershell
& .\deploy\restricted\protect-restricted-env.ps1
```

Start the loopback Mailpit instance. When Docker Desktop is unavailable, use
the pinned standalone Windows binary and then validate the complete single-host
profile:

```powershell
& .\deploy\mailpit\install-standalone.ps1
& .\deploy\mailpit\start-standalone.ps1
& .\deploy\restricted\check-restricted-readiness.ps1 -Component all
```

The command returns only non-secret identities, limits and dependency status.

## Loopback stack orchestration

The restricted stack does not install Windows services. It uses a pinned Caddy
v2.11.4 binary, builds the static React application and starts Mailpit, API,
the real-queue Worker and Caddy as hidden processes:

```powershell
& .\deploy\restricted\start-restricted-stack.ps1
```

The only browser entry point is `http://127.0.0.1:8080`; Mailpit remains at
`http://127.0.0.1:8025`. API port 8000 is also bound only to IPv4 loopback and
is intended solely as Caddy's upstream. Caddy serves loopback HTTP without an
HSTS header; this exception must not be reused for a LAN or public deployment.

The first run validates or downloads the fixed Caddy release. Normal starts
reuse installed frontend dependencies and rebuild `apps/web/dist`. Use
`-InstallWebDependencies` only when `node_modules` must be recreated. After a
verified build, `-SkipWebBuild` can be used for a restart.

Inspect the non-secret process and endpoint report with:

```powershell
& .\deploy\restricted\get-restricted-stack-status.ps1 -RequireReady
```

Stop the complete stack with:

```powershell
& .\deploy\restricted\stop-restricted-stack.ps1
```

Caddy, Worker and API are stopped in that order, followed by Mailpit. PostgreSQL
is an installed local dependency and remains running. Mailpit messages and all
logs remain under `D:\3Dreconstruction\Re3D-data\_services`. Managed process
state records the PID, executable path and process start time; a mismatched
identity is reported and is never terminated. Windows process-tree shutdown is
forceful, so a running Worker job relies on the existing database lease expiry
and recovery contract. Do not use the stop script as normal job cancellation.
