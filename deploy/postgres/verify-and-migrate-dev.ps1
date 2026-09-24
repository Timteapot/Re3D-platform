$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

if (-not (Test-Path -LiteralPath $psql -PathType Leaf)) {
    throw "PostgreSQL 18 psql.exe was not found at: $psql"
}

Write-Host "Re3D Platform development database verification" -ForegroundColor Cyan
$securePassword = Read-Host "Password for re3d_app" -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)

try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    $encodedPassword = [Uri]::EscapeDataString($plainPassword)
    $env:PGPASSWORD = $plainPassword
    $env:DATABASE_URL = (
        "postgresql+psycopg://re3d_app:{0}@127.0.0.1:5432/re3d_platform_dev" `
        -f $encodedPassword
    )
    Remove-Item Env:RE3D_TEST_DATABASE_URL -ErrorAction SilentlyContinue

    Write-Host "Checking role flags and development database privileges..."
    $verificationSql = @"
SELECT current_database() AS database_name, current_user AS role_name;
SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit,
       rolreplication, rolbypassrls, rolconnlimit
FROM pg_roles
WHERE rolname = current_user;
DO `$verification`$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_auth_members AS membership
        JOIN pg_roles AS member ON member.oid = membership.member
        WHERE member.rolname = current_user
    ) THEN
        RAISE EXCEPTION 're3d_app must not be a member of another role';
    END IF;
END
`$verification`$;
SELECT
    has_database_privilege(current_user, current_database(), 'CONNECT') AS can_connect,
    has_database_privilege(current_user, current_database(), 'TEMPORARY') AS can_create_temp,
    has_schema_privilege(current_user, 'public', 'USAGE') AS can_use_public,
    has_schema_privilege(current_user, 'public', 'CREATE') AS can_create_in_public;
DROP TABLE IF EXISTS public.__re3d_permission_probe;
CREATE TABLE public.__re3d_permission_probe (id integer PRIMARY KEY);
DROP TABLE public.__re3d_permission_probe;
"@
    & $psql `
        -X `
        -v ON_ERROR_STOP=1 `
        -h 127.0.0.1 `
        -p 5432 `
        -U re3d_app `
        -d re3d_platform_dev `
        -c $verificationSql
    if ($LASTEXITCODE -ne 0) {
        throw "Application role verification failed with exit code $LASTEXITCODE"
    }

    Write-Host "Applying Alembic migrations as re3d_app..."
    Push-Location $projectRoot
    try {
        & $python -m alembic -c alembic.ini upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "Alembic migration failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }

    Write-Host "Checking migrated schema..."
    $migrationSql = @"
SELECT version_num FROM alembic_version;
SELECT resource_key, job_id, worker_id, expires_at FROM worker_leases;
SELECT count(*) AS development_job_count FROM reconstruction_jobs;
SELECT count(*) AS development_user_count FROM users;
SELECT count(*) AS open_upload_count
FROM job_uploads
WHERE status = 'uploading';
SELECT count(*) AS uploaded_image_count FROM job_upload_images;
SELECT count(*) AS active_refresh_session_count
FROM refresh_sessions
WHERE revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP;
"@
    & $psql `
        -X `
        -v ON_ERROR_STOP=1 `
        -h 127.0.0.1 `
        -p 5432 `
        -U re3d_app `
        -d re3d_platform_dev `
        -c $migrationSql
    if ($LASTEXITCODE -ne 0) {
        throw "Migrated schema verification failed with exit code $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "Verification and migration completed successfully." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    $scriptExitCode = 1
} finally {
    $env:PGPASSWORD = $null
    $env:DATABASE_URL = $null
    $plainPassword = $null
    $encodedPassword = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
}

Read-Host "Press Enter to close this window"
if ($scriptExitCode) {
    exit $scriptExitCode
}
