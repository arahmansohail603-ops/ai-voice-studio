# Starts AI Voice Studio for local development from a desktop shortcut.
# Loads scripts\license-dev.env, makes sure the local license server is
# reachable, launches the app without a console window and reports any startup
# failure through a message box plus %LOCALAPPDATA%\AI Voice Studio\startup.log.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ServerDir = Join-Path $ProjectRoot 'server'
$ServerPython = Join-Path $ServerDir '.venv\Scripts\pythonw.exe'
$LogDir = Join-Path $env:LOCALAPPDATA 'AI Voice Studio'
$LogPath = Join-Path $LogDir 'startup.log'
$ServerHost = '127.0.0.1'
$ServerPort = 8000

function Write-Log {
    param([string]$Message)
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) {
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        }
        if ((Test-Path -LiteralPath $LogPath) -and (Get-Item -LiteralPath $LogPath).Length -gt 1MB) {
            Move-Item -LiteralPath $LogPath -Destination "$LogPath.old" -Force
        }
        Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    } catch {
    }
}

function Show-StartupError {
    param([string]$Message)
    Write-Log "FAILED $Message"
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        [void][System.Windows.Forms.MessageBox]::Show(
            $Message,
            'AI Voice Studio could not start',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        )
    } catch {
    }
    exit 1
}

function Import-DevEnvironment {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $entry = $line.Trim()
        if (-not $entry -or $entry.StartsWith('#') -or -not $entry.Contains('=')) {
            continue
        }
        $pair = $entry -split '=', 2
        [Environment]::SetEnvironmentVariable($pair[0].Trim(), $pair[1].Trim(), 'Process')
    }
}

function Test-Endpoint {
    param([string]$Uri)
    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 4
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Start-LicenseServer {
    $health = "http://$ServerHost`:$ServerPort/health/"
    if (Test-Endpoint $health) {
        return
    }
    if (-not (Test-Path -LiteralPath $ServerPython)) {
        Show-StartupError (
            "The license server is not running and its virtual environment is missing.`n`n" +
            "Run: python -m venv server\.venv; server\.venv\Scripts\python -m pip install -r server\requirements.txt`n`n" +
            "Then start it with: server\.venv\Scripts\python server\manage.py runserver 127.0.0.1:8000`n`n" +
            "Log: $LogPath"
        )
    }
    Write-Log "Starting the license server on $ServerHost`:$ServerPort"
    $serverOut = Join-Path $LogDir 'server-stdout.log'
    $serverErr = Join-Path $LogDir 'server-stderr.log'
    Start-Process -FilePath $ServerPython `
        -ArgumentList @('manage.py', 'runserver', "$ServerHost`:$ServerPort", '--noreload', '--insecure') `
        -WorkingDirectory $ServerDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $serverOut `
        -RedirectStandardError $serverErr | Out-Null
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        Start-Sleep -Milliseconds 750
        if (Test-Endpoint $health) {
            Write-Log 'License server is ready'
            return
        }
    }
    $details = ''
    if (Test-Path -LiteralPath $serverErr) {
        $details = (Get-Content -LiteralPath $serverErr -Tail 8) -join "`n"
    }
    Show-StartupError "The license server did not start on $ServerHost`:$ServerPort.`n`n$details`n`nLog: $LogPath"
}

Import-DevEnvironment (Join-Path $PSScriptRoot 'license-dev.env')
Write-Log 'Launcher started'

if (-not (Test-Path -LiteralPath $Python)) {
    Show-StartupError "The application virtual environment is missing.`n`nExpected: $Python`n`nCreate it with: python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt`n`nLog: $LogPath"
}

Start-LicenseServer

$stdout = Join-Path $LogDir 'app-stdout.log'
$stderr = Join-Path $LogDir 'app-stderr.log'
# No -WindowStyle Hidden here: the hidden console is inherited from this
# launcher, and a hidden start state would leave the Qt main window invisible.
# The entry script is passed as an absolute path because a relative script path
# resolves through the project folder, which sits on a OneDrive reparse point.
$process = Start-Process -FilePath $Python `
    -ArgumentList @("`"$ProjectRoot\main.py`"") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Start-Sleep -Seconds 4
if ($process.HasExited) {
    $details = ''
    if (Test-Path -LiteralPath $stderr) {
        $details = (Get-Content -LiteralPath $stderr -Tail 12) -join "`n"
    }
    if ($details -match 'already running') {
        Write-Log 'AI Voice Studio is already running'
        exit 0
    }
    Show-StartupError "AI Voice Studio exited with code $($process.ExitCode).`n`n$details`n`nLog: $LogPath"
}

Write-Log "AI Voice Studio started (pid $($process.Id))"
exit 0
