# Restricted deployment profile

This profile runs the real Re3D pipeline on one Windows host while
keeping the web application, API, PostgreSQL and Mailpit on loopback. It is for
local acceptance or authenticated SSH-tunnel access, not for direct LAN or public
Internet exposure. Remote browser access uses a local loopback port forwarded to
the server's loopback listener; it does not publish the web service.

## Current acceptance scope

The target Windows GPU host passed the four-service baseline acceptance on
2026-10-13. The later service bundle at platform commit `29eda46` also completed
a browser-submitted 11-image real reconstruction, three-branch artifact and
evaluation checks, and automatic Three.js canvas display without refreshing the
task-detail page. These are restricted-deployment results, not public-production
or 300-image capacity acceptance. The exact evidence and coverage limitations are
recorded in
[`docs/target-server-restricted-acceptance-2026-10-13.md`](../../docs/target-server-restricted-acceptance-2026-10-13.md).

The direct 300-image experiment preset, input validation and offline release
preparation are available locally; the new preset has not yet been applied to
the target host. Application order and resource admission requirements are in
[`docs/direct-300-local-readiness-2026-10-13.md`](../../docs/direct-300-local-readiness-2026-10-13.md).
The default local `baseline` configuration remains separate from this experiment.

## Enforced boundary

- `APP_ENV=restricted` is distinct from development, test and production.
- API and public browser origins must use `localhost`, `127.0.0.0/8` or `::1`.
- trusted proxy networks and SMTP must remain on loopback.
- stable upload routes create only `real` jobs; development-only routes are absent.
- rate, transfer, task and user-storage limits must all be explicit.
- PostgreSQL must use database `re3d_platform_restricted` and the non-admin role
  `re3d_restricted_runtime` on loopback.
- task data must be outside the source checkout and meet the configured free-space
  floor. Successful input, runtime and artifact retention thresholds are each
  30 days. Scheduled maintenance records dry-run results only; it does not
  automatically delete successful results. Actual deletion requires explicit
  operator confirmation. Failed and cancelled task files use a separate cleanup
  policy after its configured grace period.

## Initialization order

First validate the database contract without touching the installed PostgreSQL:

```powershell
& .\deploy\postgres\run-restricted-database-acceptance.ps1
```

For a first-time single-host installation, use the unified initializer. It asks
only for the PostgreSQL administrator password, validates it before making any
change, generates separate random migration/runtime passwords, applies the
migrations, creates protected environment files, starts loopback Mailpit and
runs the complete readiness check. On the target server, run this command from
the non-elevated local `re3d-deploy` session so that the protected source
environment files retain the owner expected by later elevated bundle generation:

```powershell
& .\deploy\restricted\initialize-server-restricted.ps1
```

If the server's original `postgres` password is unknown, do not put a guessed
password in a script or environment file. Run the elevated, rollback-protected
recovery entry point through the cloud provider's SYSTEM/administrator command
channel, then consume its short-lived DPAPI envelope as `re3d-deploy`:

```powershell
# Elevated cloud-assistant command
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File D:\Re3D-platform\deploy\postgres\recover-admin-password.ps1

# SSH session running as re3d-deploy
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File D:\Re3D-platform\deploy\restricted\initialize-server-from-recovery-envelope.ps1 -EnvelopePath D:\Re3D-data\_recovery\credentials\postgres-admin-password.dpapi
```

The recovery entry point takes a byte-identical HBA backup, freezes a
self-contained rollback helper in an administrator-only run directory, and
installs a SYSTEM watchdog that executes only that hash-checked snapshot before
opening one exact loopback rule. It validates the PostgreSQL 18 `libpq` library
before changing HBA, changes the password with `PQchangePassword`, restores the
original HBA and verifies SCRAM password authentication with
`PQconnectionUsedPassword` before creating the envelope. The consumer validates its protected ACL,
machine, Windows SID, database endpoint, role and expiry, then deletes it before
database initialization. It never persists the administrator password in the
restricted environment. Detailed failure and recovery behavior is documented in
[`deploy/postgres/README.md`](../postgres/README.md).

The initializer refuses to run when either restricted environment file already
exists, so retrying cannot silently rotate the database password away from the
saved runtime configuration. For manual recovery or component-by-component
setup, initialize PostgreSQL with different passwords of at least 16 characters:

```powershell
& .\deploy\postgres\bootstrap-restricted.ps1
& .\deploy\postgres\migrate-restricted.ps1
```

Finally create the ignored API/Worker environment files and external data root.
Enter the same runtime password used above:

```powershell
& .\deploy\restricted\initialize-restricted-env.ps1
```

Password-taking initialization entry points accept `SecureString` parameters
for automation and do not print passwords. Administrator and migration passwords
are not saved in the restricted environment files; the recovery path uses only
the short-lived encrypted envelope described above. The runtime password and a
generated JWT secret are saved in the ignored local environment files.
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

## Interactive loopback stack orchestration

The basic restricted-stack command does not install Windows services. It uses a
pinned Caddy v2.11.4 binary, builds the static React application and starts
Mailpit, API, the real-queue Worker and Caddy as hidden processes. A direct
interactive start is suitable for a local PowerShell session:

```powershell
& .\deploy\restricted\start-restricted-stack.ps1
```

Processes created by a non-interactive Windows SSH session are tied to that
session and must not be treated as a durable deployment.

## Durable restricted Windows services

The recommended passwordless lifecycle for an SSH-managed Windows server is a
four-service WinSW bundle. Mailpit, API, Worker and Caddy run under separate
Windows virtual service accounts:

- `NT SERVICE\Re3DRestrictedMailpit`;
- `NT SERVICE\Re3DRestrictedApi`;
- `NT SERVICE\Re3DRestrictedWorker`;
- `NT SERVICE\Re3DRestrictedCaddy`.

This separation is intentional. In particular, the Worker service receives
only the Worker environment and cannot read the API JWT, cookie or SMTP
secrets. No account password is stored in a WinSW XML or manifest. The services
are installed with Manual start mode so a server reboot or cloud savings-stop
resume does not silently begin consuming queued GPU work. WinSW failure recovery
still applies after an operator explicitly starts the bundle.

Build and verify the frontend first. Then create the commit-bound runtime bundle
outside the Git checkout. The following example resolves the default pinned
Caddy and Mailpit locations relative to the current project checkout:

```powershell
$projectRoot = (Resolve-Path .).Path
$workspaceRoot = Split-Path -Parent $projectRoot

& .\deploy\production\build-web.ps1 -SkipInstall

& .\deploy\restricted\windows-services\new-service-bundle.ps1 `
    -OutputRoot C:\ProgramData\Re3DRestricted `
    -WinSWPath C:\Install\WinSW-x64.exe `
    -CaddyPath (Join-Path $workspaceRoot 'tools\caddy\v2.11.4\caddy.exe') `
    -MailpitPath (Join-Path $workspaceRoot 'tools\mailpit\v1.31.3\mailpit.exe')

& .\deploy\restricted\windows-services\test-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DRestricted `
    -CheckExternalBinaryVersions
```

Bundle generation and installation both run from an elevated Administrator
PowerShell. The generator copies the API and Worker environment files into
separate protected locations in the bundle. Installation validates ACL
boundaries and registers the four services but does not start them. The two
source environment files must remain owned by the fixed local
`COMPUTERNAME\re3d-deploy` account created by the initialization flow; elevation
does not broaden that source-secret owner contract. The original WinSW download
is verified while generating the bundle, then each copied wrapper is verified
independently, so `C:\Install\WinSW-x64.exe` is not a runtime dependency.

The service manifest schema is `1.1`. It pins both the project virtual
environment launcher (`.venv\Scripts\python.exe`) and the Windows runtime image
declared by `.venv\pyvenv.cfg` and reported by `sys._base_executable`, including
their SHA-256 values. Before invoking Python during bundle validation, the
validator checks the fixed virtual-environment path, `pyvenv.cfg`, the declared
base interpreter and their required parent directories for broad write access.
Installation repeats the no-broad-write check for the actual runtime image and
its parent. Windows can expose either the virtual-environment launcher or the
base interpreter in a Python process chain; installed-service acceptance allows
only those two pinned images and requires the command line to begin with the
same image reported for that process, followed by the exact API or Worker
module arguments. Any interpreter, configuration or hash drift requires a new
bundle. Legacy `1.0` service manifests are rejected rather than inferred.

The install-time API readiness check validates the complete restricted SMTP
configuration but deliberately defers network connectivity because the new
Mailpit service is not running yet. The installed-service acceptance below
performs the real SMTP protocol check after it has verified that ports 1025 and
8025 belong to the new Mailpit service.

Worker `TEMP`/`TMP` defaults to
`RE3D_DATA_ROOT\_services\restricted-worker-temp` on the data disk, rather
than `C:\ProgramData`; `-WorkerTempRoot` may override it only with another
path inside `RE3D_DATA_ROOT`.

```powershell
& .\deploy\restricted\windows-services\install-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DRestricted

# Required when the older standalone/interactive stack was used previously.
& .\deploy\mailpit\stop-standalone.ps1

& .\deploy\restricted\windows-services\start-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DRestricted

& .\deploy\restricted\windows-services\test-installed-services.ps1 `
    -BundleRoot C:\ProgramData\Re3DRestricted
```

The original four-service baseline at platform commit
`a202d77274b7aab0ed6525f0903ef973c0a193b7` verified the loopback service boundary
and Worker queue loop. The later `29eda46` result added the real 11-image browser
workflow described in the current acceptance scope. Server reboot recovery,
300-image capacity and public production readiness remain separate uncompleted
acceptance items.

The service bundle records a clean platform commit. Explicit install, start and
acceptance commands refuse source drift, but an already running WinSW service
does not revalidate Git state during automatic process restart. The services
still execute code from that checked-out project directory. This is acceptable
only for the restricted acceptance profile. A public production deployment must
use the separate production release/service process and an
administrator-controlled immutable release directory.

Start order is Mailpit, API, Worker and Caddy. Stop and uninstall order is the
reverse, so the public loopback entry point closes before its dependencies.
Before stopping the cloud instance, first ensure no reconstruction is active,
then stop and verify the bundle:

```powershell
& .\deploy\restricted\windows-services\stop-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DRestricted

& .\deploy\restricted\windows-services\get-service-bundle-status.ps1 `
    -BundleRoot C:\ProgramData\Re3DRestricted
```

An elevated Administrator can remove only the service registrations with
`uninstall-service-bundle.ps1`. The bundle, logs, Mailpit state, task data and
non-secret ACL entries are retained for audit and recovery; the uninstaller
removes stale service-SID access from the two secret files and never recursively
deletes those paths.

## Task Scheduler compatibility path

The older S4U Scheduled Task path remains available for compatible local
Windows hosts, but it is not the recommended server lifecycle. On the current
target server, repeated minimal S4U registration tests returned
`E_ACCESSDENIED` from an OpenSSH network-logon session even after the exact
account received `SeBatchLogonRight`; an InteractiveToken definition could be
registered but could not be dispatched from that session. Do not weaken the
identity model to `LocalSystem`, store a user password, or grant administrator
membership to bypass that host-specific result.

To evaluate the compatibility path, first provision a dedicated Task Scheduler
folder from an elevated Administrator PowerShell. The provisioner performs two
narrowly scoped operations:

- it creates or validates a protected scheduler-folder ACL that grants access
  only to SYSTEM, Administrators and the exact local `re3d-deploy` account;
- it grants that account `SeBatchLogonRight` (log on as a batch job), which is
  required for the task's S4U logon. This is a user right, not membership in
  Administrators, and the script refuses to proceed if the account is an
  Administrator or if `SeDenyBatchLogonRight` has any effective assignment.

The right is changed directly with the Windows LSA API; the provisioner does
not import or replace the machine's security-policy template. If the right was
already present, it is treated as externally managed. If it was absent, the
script writes a protected SYSTEM/Administrators-only ownership marker under
`HKLM\SOFTWARE\Re3D\RestrictedDeployment` before adding exactly that one right,
then records completion only after the folder is validated. A repeated run can
recover the intermediate marker after an interrupted attempt:

```powershell
& .\deploy\restricted\provision-restricted-task-folder.ps1
```

Then return to a PowerShell session running as `re3d-deploy` itself and install
the passwordless, on-demand task. It runs as that exact local identity with an
S4U limited token, stores no account password and has no startup trigger or
automatic restart policy:

```powershell
& .\deploy\restricted\install-restricted-scheduled-task.ps1
& .\deploy\restricted\start-restricted-scheduled-task.ps1
& .\deploy\restricted\get-restricted-scheduled-task-status.ps1 -RequireReady
```

The task deliberately uses `-SkipWebBuild`; build and verify the frontend
before installing it. It also cannot use remote shares or fetch missing runtime
assets under S4U, so Python environments, model files and binaries must already
exist on local disks. Closing SSH does not stop the scheduled stack. Create an
SSH tunnel only when browser access is needed:

```powershell
ssh -N `
    -L 127.0.0.1:8080:127.0.0.1:8080 `
    -L 127.0.0.1:8025:127.0.0.1:8025 `
    re3d-aliyun
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

For the Scheduled Task variant, stop the supervisor first and then converge any
remaining or stale child-process state with the dedicated command:

```powershell
& .\deploy\restricted\stop-restricted-scheduled-task.ps1
```

The same `re3d-deploy` identity can remove only the task registration while
retaining all data and logs:

```powershell
& .\deploy\restricted\uninstall-restricted-scheduled-task.ps1
```

If status reports `unsafe-definition`, normal lifecycle commands deliberately
refuse the drifted task. After inspecting the task, the same fixed local
identity can stop and delete only `\Re3D\RestrictedStack` with:

```powershell
& .\deploy\restricted\uninstall-restricted-scheduled-task.ps1 `
    -ForceUnsafeDefinition
```

After the task has been removed, an elevated Administrator can optionally remove
the empty, ACL-validated scheduler folder. The same command removes
`SeBatchLogonRight` only when the protected marker proves that this provisioner
added it; a pre-existing or externally managed grant is retained. If revocation
fails, the marker is deliberately retained so a later run can recover:

```powershell
& .\deploy\restricted\deprovision-restricted-task-folder.ps1
```

The delegated folder lets `re3d-deploy` create and manage tasks that run only as
itself; it does not permit passwordless registration as SYSTEM or another user.
The batch-logon right likewise does not grant interactive logon, administrator
membership or permission to use remote resources. A local/domain policy refresh
can still remove the allow right or introduce a deny assignment; task
installation then fails closed and an Administrator must review the effective
policy rather than bypassing it.
The current scripts accept only the fixed `RestrictedStack` task definition and
refuse definition or ACL drift during normal operation. The explicit recovery
switch still requires the exact `re3d-deploy` identity and a valid protected
folder ACL; it cannot target another task name.

Caddy, Worker and API are stopped in that order, followed by Mailpit. PostgreSQL
is an installed local dependency and remains running. Mailpit messages and all
logs remain under the workspace sibling `Re3D-data\_services` directory. Managed process
state records the PID, executable path and process start time; a mismatched
identity is reported and is never terminated. Windows process-tree shutdown is
forceful, so a running Worker job relies on the existing database lease expiry
and recovery contract. Do not use the stop script as normal job cancellation.
The on-demand task is a temporary lifecycle layer for restricted acceptance; it
does not replace the WinSW service isolation, startup recovery and monitoring
required by the production profile.
