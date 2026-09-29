# One-time local setup for AI Voice Studio.
#
# Creates both virtual environments, generates a fresh Ed25519 signing key pair
# for this machine, configures the local license server, migrates its database
# and issues a license key that is bound to this computer.
#
# Nothing secret is ever committed: the signing private key, the Django secret
# and the HMAC pepper all stay in server\.env, and the public trust
# configuration stays in scripts\license-dev.env. Both files are git-ignored.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $Root 'server'
$AppVenv = Join-Path $Root '.venv'
$ServerVenv = Join-Path $ServerDir '.venv'
$ServerEnvFile = Join-Path $ServerDir '.env'
$DevEnvFile = Join-Path $PSScriptRoot 'license-dev.env'
$KeyFile = Join-Path $Root 'LICENSE-KEY.txt'
$KeyId = 'local-v1'
$LASTEXITCODE = 0

function Write-Step($Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked($Description, [scriptblock]$Action) {
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit code $LASTEXITCODE)."
    }
}

# Windows PowerShell's Set-Content -Encoding utf8 writes a byte order mark, which
# corrupts the first key name when Python reads the file with plain "utf-8".
# These files are always written as UTF-8 without a BOM.
function Write-Utf8File($Path, [string[]]$Lines) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Lines -join [Environment]::NewLine) + [Environment]::NewLine, $encoding)
}

# Resolves a concrete python executable on 3.10-3.12, preferring the py launcher
# when it points at a supported version.
$script:PythonExe = $null
$script:PythonPrefix = @()

function Resolve-Python {
    $attempts = @()
    $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($py) { $attempts += , @($py.Source, @('-3')) }
    $plain = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($plain) { $attempts += , @($plain.Source, @()) }

    foreach ($attempt in $attempts) {
        $exe = $attempt[0]
        $prefix = $attempt[1]
        $probe = & $exe @prefix -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $probe) { continue }
        $parts = "$probe".Trim().Split('.')
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        if ($major -eq 3 -and $minor -ge 10 -and $minor -le 12) {
            $script:PythonExe = $exe
            $script:PythonPrefix = $prefix
            return
        }
    }
    throw 'Python 3.10, 3.11 or 3.12 is required but was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".'
}

Write-Step 'Checking Python'
Resolve-Python
$versionText = (& $script:PythonExe @script:PythonPrefix -c 'import sys; print(sys.version.split()[0])').Trim()
Write-Host "    using: $script:PythonExe (Python $versionText)"

Write-Step 'Creating the application virtual environment'
if (-not (Test-Path -LiteralPath (Join-Path $AppVenv 'Scripts\python.exe'))) {
    Invoke-Checked 'virtual environment creation' {
        & $script:PythonExe @script:PythonPrefix -m venv $AppVenv
    }
}
$AppPython = Join-Path $AppVenv 'Scripts\python.exe'
Invoke-Checked 'dependency install' {
    & $AppPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
}
Write-Host '    application dependencies ready'

Write-Step 'Creating the license server virtual environment'
if (-not (Test-Path -LiteralPath (Join-Path $ServerVenv 'Scripts\python.exe'))) {
    Invoke-Checked 'server virtual environment creation' {
        & $script:PythonExe @script:PythonPrefix -m venv $ServerVenv
    }
}
$ServerPython = Join-Path $ServerVenv 'Scripts\python.exe'
Invoke-Checked 'server dependency install' {
    & $ServerPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $ServerDir 'requirements.txt')
}
Write-Host '    server dependencies ready'

Write-Step 'Generating the signing key pair'
$helperSource = @'
import base64
import json
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def public_of(private_b64):
    raw = base64.urlsafe_b64decode(private_b64 + "=" * (-len(private_b64) % 4))
    key = Ed25519PrivateKey.from_private_bytes(raw)
    return encode(key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ))


def generate():
    key = Ed25519PrivateKey.generate()
    private = encode(key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return {
        "private": private,
        "public": public_of(private),
        "django_secret": encode(os.urandom(48)),
        "pepper": encode(os.urandom(48)),
    }


if sys.argv[1] == "derive":
    print(public_of(sys.argv[2]))
else:
    print(json.dumps(generate()))
'@

# PowerShell 5.1 strips double quotes from strings passed to native commands, so
# the helper is written to a file instead of being passed with -c.
$helper = Join-Path ([System.IO.Path]::GetTempPath()) ("avis-keys-" + [guid]::NewGuid().ToString('N') + ".py")
Set-Content -LiteralPath $helper -Value $helperSource -Encoding utf8

$private = $null
$public = $null
$djangoSecret = $null
$pepper = $null
$reusedExistingKey = $false

try {
    if (Test-Path -LiteralPath $ServerEnvFile) {
        $found = Select-String -LiteralPath $ServerEnvFile -Pattern '^LICENSE_SIGNING_KEYS=\{"[^"]*":"([^"]+)"' |
            Select-Object -First 1
        if ($found) {
            $private = $found.Matches[0].Groups[1].Value
            $reusedExistingKey = $true
        }
    }

    if ($reusedExistingKey) {
        $public = (& $AppPython $helper 'derive' $private)
        if ($LASTEXITCODE -ne 0) { throw 'Deriving the public key failed.' }
        $public = "$public".Trim()
        Write-Host '    reusing the key pair already present in server\.env'
    }
    else {
        $raw = (& $AppPython $helper 'generate')
        if ($LASTEXITCODE -ne 0) { throw 'Signing key generation failed.' }
        $generated = $raw | ConvertFrom-Json
        $private = $generated.private
        $public = $generated.public
        $djangoSecret = $generated.django_secret
        $pepper = $generated.pepper
    }
}
finally {
    Remove-Item -LiteralPath $helper -Force -ErrorAction SilentlyContinue
}

Write-Host "    public key id: $KeyId"
Write-Host "    public key   : $public"

Write-Step 'Configuring the local license server'
if (-not $reusedExistingKey) {
    $signingKeys = '{"' + $KeyId + '":"' + $private + '"}'
    $serverEnv = @(
        "DJANGO_SECRET_KEY=$djangoSecret"
        'DJANGO_DEBUG=0'
        'DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost'
        'DJANGO_SECURE_SSL_REDIRECT=0'
        'DJANGO_SESSION_COOKIE_SECURE=0'
        'DJANGO_CSRF_COOKIE_SECURE=0'
        'DJANGO_SECURE_HSTS_SECONDS=0'
        'DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=0'
        'DJANGO_SECURE_HSTS_PRELOAD=0'
        'DATABASE_ENGINE=django.db.backends.sqlite3'
        'DATABASE_NAME=db.sqlite3'
        "LICENSE_HMAC_PEPPER=$pepper"
        "LICENSE_SIGNING_KEY_ID=$KeyId"
        "LICENSE_SIGNING_KEYS=$signingKeys"
        'LICENSE_LEASE_DURATION_SECONDS=3600'
        'LICENSE_LEASE_GRACE_SECONDS=300'
        'LICENSE_NONCE_TTL_SECONDS=900'
        'LICENSE_IDEMPOTENCY_TTL_SECONDS=86400'
        'LICENSE_LEASE_RETENTION_COUNT=10'
        'LICENSE_RATE_LIMIT_ACTIVATION=10/minute'
        'LICENSE_RATE_LIMIT_STATUS=60/minute'
        'LICENSE_RATE_LIMIT_HEALTH=120/minute'
        'LICENSE_CACHE_BACKEND=django.core.cache.backends.locmem.LocMemCache'
        'LICENSE_CACHE_LOCATION=license-server'
        'LICENSE_TRUST_PROXY_HEADERS=0'
    )
    Write-Utf8File $ServerEnvFile $serverEnv
    Write-Host '    wrote server\.env (git-ignored, never committed)'
}

$devEnv = @(
    '# Development trust configuration for the desktop launcher.'
    '# Only public keys belong here. The signing private key and the HMAC pepper'
    '# stay in server\.env and must never be copied into the desktop app.'
    '# Generated by scripts\setup-dev.ps1.'
    'AI_VOICE_STUDIO_LICENSE_URL=http://127.0.0.1:8000'
    ('AI_VOICE_STUDIO_LICENSE_PUBLIC_KEYS={"' + $KeyId + '":"' + $public + '"}')
)
Write-Utf8File $DevEnvFile $devEnv
Write-Host '    wrote scripts\license-dev.env (git-ignored, never committed)'

Write-Step 'Creating the license server database'
$licenseKey = ''
Push-Location $ServerDir
try {
    Invoke-Checked 'database migration' { & $ServerPython 'manage.py' migrate --noinput }
    Write-Step 'Issuing a license key for this computer'
    $keyOutput = & $ServerPython 'manage.py' shell -c "from licenses.models import LicenseKey; print(LicenseKey.issue()[1])"
    if ($LASTEXITCODE -ne 0) { throw 'Issuing the license key failed.' }
    # manage.py shell also prints an import banner, so take the LIC- line only.
    $licenseKey = ($keyOutput | Where-Object { "$_".Trim() -match '^LIC-' } | Select-Object -First 1)
    if ($licenseKey) { $licenseKey = "$licenseKey".Trim() }
}
finally {
    Pop-Location
}

if (-not $licenseKey) {
    throw 'The license server did not return a license key.'
}

$keyLines = @(
    'AI Voice Studio - local license key'
    'Issued for this computer only. Do not commit or share this file.'
    ''
    $licenseKey
)

Write-Host "    issued: $licenseKey"
Write-Utf8File $KeyFile $keyLines
Write-Host '    also written to LICENSE-KEY.txt (git-ignored)'

Write-Step 'Setup complete'
Write-Host 'Start the app with:' -ForegroundColor Green
Write-Host ''
Write-Host '    powershell -ExecutionPolicy Bypass -File "scripts\start-app.ps1"' -ForegroundColor Green
Write-Host ''
Write-Host 'It starts the license server automatically. Paste the key above if a' -ForegroundColor Green
Write-Host 'dialog asks for one.' -ForegroundColor Green
Write-Host ''
