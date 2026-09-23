<#
.SYNOPSIS
    First-run setup for Jarvis on Windows: checks the prerequisites, installs
    the dependencies, and tells you exactly what is still missing.

.DESCRIPTION
    Everything a freshly cloned copy needs before `python -m jarvis` can work,
    in the order it has to happen. Each step either fixes the problem itself or
    prints the one command that does.

    This runs BEFORE the virtualenv exists, so it is plain PowerShell with no
    dependencies of its own. Once it finishes, `--doctor` (which does need the
    venv) takes over and checks the runtime side: the microphone Jarvis can
    actually open, the wake-word model, the Gemini connection.

.PARAMETER SkipInstall
    Only report. Do not create the virtualenv or install anything.

.PARAMETER SkipDoctor
    Do not run `python -m jarvis --doctor` at the end.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup.ps1

    The form to use on a machine that has never run a local script before --
    Windows blocks unsigned .ps1 files by default, and -ExecutionPolicy Bypass
    applies to this one process only, changing nothing permanently.
#>

[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$SkipDoctor
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

# Run as if you had cd'd into the project first. `python -m jarvis` puts the
# CURRENT directory at the front of sys.path, so launching this script by its
# full path from somewhere else -- powershell -File C:\dev\jarvis-windows\setup.ps1
# from your home folder, say -- would import whatever `jarvis` package happens
# to sit in that other directory, and --doctor would then report on the wrong
# copy: its key, its config, its Telegram session. Everything below uses $root
# explicitly, but the Python it launches needs the working directory too.
Push-Location $root
trap { Pop-Location; break }

# Every hard failure is collected rather than thrown, so one run tells you
# everything that is wrong instead of making you discover it a step at a time.
$blockers = New-Object System.Collections.ArrayList
$notes = New-Object System.Collections.ArrayList

function Write-Step([string]$text) { Write-Host "`n$text" -ForegroundColor Cyan }
function Write-Ok([string]$text)   { Write-Host "  [ok]   $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  [opt]  $text" -ForegroundColor Yellow }
function Write-Bad([string]$text)  { Write-Host "  [!!]   $text" -ForegroundColor Red }

function Add-Blocker([string]$what, [string]$fix) {
    Write-Bad $what
    Write-Host "         fix: $fix" -ForegroundColor DarkGray
    [void]$blockers.Add("$what`n     fix: $fix")
}

Write-Host ""
Write-Host "  JARVIS - setup check" -ForegroundColor White
Write-Host "  $root" -ForegroundColor DarkGray

# ---------------------------------------------------------------- 1. Windows
Write-Step "1/6  Windows"

if ($env:OS -ne 'Windows_NT') {
    Add-Blocker "This is not Windows." "Jarvis drives PowerShell, the Windows clipboard and toasts. It needs Windows 10 or 11."
} else {
    $os = (Get-CimInstance Win32_OperatingSystem).Caption
    $build = [System.Environment]::OSVersion.Version.Build
    if ($build -lt 10240) {
        Add-Blocker "$os (build $build) is older than Windows 10." "Upgrade to Windows 10 or 11."
    } else {
        Write-Ok "$os (build $build)"
    }
}

# Windows caps a path at 260 characters unless long paths are switched on, and
# the deepest file pip unpacks here -- inside PySide6's bundled QML assets --
# is 176 characters on its own. That leaves only ~84 characters for the folder
# you cloned into, which a redirected OneDrive Documents folder can exceed on
# its own. Without this check the install dies two minutes in, deep in pip
# output, as "[WinError 206] The filename or extension is too long".
$deepestBundledFile = 176
$pathBudget = 260 - $deepestBundledFile
$longPaths = 0
try {
    $lpValue = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction Stop
    $longPaths = [int]$lpValue.LongPathsEnabled
} catch {
    $longPaths = 0
}

if ($root.Length -gt $pathBudget) {
    if ($longPaths -eq 1) {
        Write-Warn "this folder is $($root.Length) characters deep (over $pathBudget); long paths are enabled, so it should still install"
    } else {
        Add-Blocker "This folder is too deep: $($root.Length) characters, and pip needs the last $deepestBundledFile for PySide6." "Clone somewhere shorter (C:\jarvis-windows), or enable long paths as Administrator: New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force"
    }
} else {
    Write-Ok "path length $($root.Length)/$pathBudget characters"
}

# PowerShell itself: every run_shell tool call goes through it.
$psVersion = $PSVersionTable.PSVersion
if ($psVersion.Major -ge 7) {
    Write-Ok "PowerShell $psVersion"
} else {
    Write-Ok "Windows PowerShell $psVersion (fine; PowerShell 7 from https://aka.ms/powershell handles Uzbek text better)"
}

# ----------------------------------------------------------------- 2. Python
Write-Step "2/6  Python 3.11 or 3.12"

# Not 3.13: openWakeWord (the local "Hey Jarvis" detector) has no wheels for it
# yet, so on a 3.13-only machine the install fails late and confusingly.
function Get-PythonVersion([string]$exe, [string[]]$prefixArgs) {
    $callArgs = @()
    if ($prefixArgs) { $callArgs += $prefixArgs }
    # The snippet deliberately contains no quotes of its own: PowerShell strips
    # inner quoting when it splats an array onto a native command, so "%d.%d"
    # would reach Python bare and raise a SyntaxError.
    $callArgs += @('-c', 'import sys; print(sys.version_info[0], sys.version_info[1])')
    try {
        $out = & $exe @callArgs
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = $out | Where-Object { $_ } | Select-Object -First 1
    if (-not $line) { return $null }
    $parts = "$line".Trim() -split '\s+'
    if ($parts.Count -lt 2) { return $null }
    return "$($parts[0]).$($parts[1])"
}

$python = $null
$pythonArgs = @()
$pythonLabel = ''

$venvUnusable = $false
if (Test-Path $venvPython) {
    $existing = Get-PythonVersion $venvPython @()
    if ($existing -eq '3.11' -or $existing -eq '3.12') {
        $python = $venvPython
        $pythonLabel = "existing .venv (Python $existing)"
    } else {
        # Never delete it here. A .venv can hold hours of wheel-building, and a
        # version probe that fails for some unrelated reason must not be able to
        # throw that away behind your back.
        $venvUnusable = $true
        $shown = $existing
        if (-not $shown) { $shown = 'an unknown version' }
        Add-Blocker ".venv already exists but runs $shown." "Remove-Item -Recurse -Force .venv   then run this script again"
    }
}

if (-not $python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @('3.12', '3.11')) {
            $found = Get-PythonVersion 'py' @("-$v")
            if ($found) { $python = 'py'; $pythonArgs = @("-$v"); $pythonLabel = "py -$v (Python $found)"; break }
        }
    }
}

if (-not $python) {
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) {
        $found = Get-PythonVersion $onPath.Source @()
        if ($found -eq '3.11' -or $found -eq '3.12') {
            $python = $onPath.Source
            $pythonLabel = "$($onPath.Source) (Python $found)"
        } else {
            [void]$notes.Add("python on PATH is $found, which Jarvis cannot use")
        }
    }
}

if ($python) {
    Write-Ok $pythonLabel
} else {
    Add-Blocker "No Python 3.11 or 3.12 found." "winget install Python.Python.3.12   (3.13 does NOT work - openWakeWord has no 3.13 build yet)"
}

# ----------------------------------------------- 3. virtualenv + dependencies
Write-Step "3/6  Virtualenv and dependencies"

if (-not $python) {
    Write-Bad "skipped - no usable Python"
} elseif ($venvUnusable) {
    Write-Bad "skipped - delete the existing .venv first (see above)"
} elseif ($SkipInstall) {
    if (Test-Path $venvPython) { Write-Ok ".venv present (-SkipInstall: not touching it)" }
    else { Write-Warn ".venv missing (-SkipInstall: not creating it)" }
} else {
    if (-not (Test-Path $venvPython)) {
        Write-Host "  creating .venv ..." -ForegroundColor DarkGray
        & $python @pythonArgs -m venv (Join-Path $root '.venv')
        if ($LASTEXITCODE -ne 0) {
            Add-Blocker "Could not create the virtualenv." "$pythonLabel -m venv .venv"
        }
    }

    if (Test-Path $venvPython) {
        Write-Host "  installing dependencies (a few minutes the first time) ..." -ForegroundColor DarkGray
        & $venvPython -m pip install --upgrade pip --quiet
        & $venvPython -m pip install -e $root
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "installed into .venv"
        } else {
            Add-Blocker "pip install failed." ".venv\Scripts\python -m pip install -e ."
        }
    }
}

# ------------------------------------------------------- 4. .env / Gemini key
Write-Step "4/6  .env and the Gemini API key"

$envPath = Join-Path $root '.env'
$envExample = Join-Path $root '.env.example'

if (-not (Test-Path $envPath)) {
    Copy-Item $envExample $envPath
    Write-Ok "created .env from .env.example"
} else {
    Write-Ok ".env already exists (left untouched)"
}

$envText = Get-Content $envPath -Raw -Encoding UTF8
# \s matches newlines too, so a pattern of GEMINI_API_KEY\s*=\s*(.*) walks
# straight past an empty "GEMINI_API_KEY=" and captures the next non-blank
# line -- the "# ---- Aisha AI ..." comment -- as if it were the key. The
# script then reported the key as set, never prompted for one, and handed the
# user a green "Prerequisites are in place" before --doctor failed with "API
# key not valid". Horizontal whitespace only, and the capture stays on the line.
$keyLine = [regex]::Match($envText, '(?m)^[ \t]*GEMINI_API_KEY[ \t]*=[ \t]*([^\r\n]*)')
$geminiKey = ''
if ($keyLine.Success) { $geminiKey = $keyLine.Groups[1].Value.Trim().Trim('"').Trim("'") }

if ($geminiKey) {
    $tail = $geminiKey.Substring([Math]::Max(0, $geminiKey.Length - 4))
    Write-Ok "GEMINI_API_KEY is set (...$tail)"
} else {
    Write-Host "  Jarvis cannot hear or speak without this one. It is free:" -ForegroundColor DarkGray
    Write-Host "  https://aistudio.google.com/apikey" -ForegroundColor DarkGray
    $entered = ''
    if (-not $SkipInstall) {
        $entered = (Read-Host "  Paste your Gemini API key now (or press Enter to do it later)").Trim()
    }
    if ($entered) {
        $updated = [regex]::Replace($envText, '(?m)^[ \t]*GEMINI_API_KEY[ \t]*=[^\r\n]*', "GEMINI_API_KEY=$entered")
        # No BOM: python-dotenv reads the file as plain UTF-8, and a BOM would
        # become part of the first key's name.
        [System.IO.File]::WriteAllText($envPath, $updated, (New-Object System.Text.UTF8Encoding($false)))
        Write-Ok "GEMINI_API_KEY written to .env"
    } else {
        Add-Blocker "GEMINI_API_KEY is empty in .env." "Get a free key at https://aistudio.google.com/apikey and put it in .env"
    }
}

# ------------------------------------------------------------- 5. microphone
Write-Step "5/6  Microphone permission"

# Windows gates desktop apps separately from Store apps, and a denied
# microphone surfaces at runtime as an empty device list rather than as
# anything that mentions permission.
$consent = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
$desktop = "$consent\NonPackaged"

function Get-ConsentValue([string]$path) {
    try { return (Get-ItemProperty -Path $path -Name Value -ErrorAction Stop).Value } catch { return $null }
}

$globalConsent = Get-ConsentValue $consent
$desktopConsent = Get-ConsentValue $desktop

if ($globalConsent -eq 'Deny') {
    Add-Blocker "Microphone access is turned off for this account." "Settings > Privacy & security > Microphone > Microphone access: On"
} elseif ($desktopConsent -eq 'Deny') {
    Add-Blocker "Desktop apps are not allowed to use the microphone." "Settings > Privacy & security > Microphone > Let desktop apps access your microphone: On"
} elseif ($globalConsent -eq 'Allow') {
    Write-Ok "allowed"
} else {
    Write-Warn "could not read the setting - Windows will ask the first time Jarvis listens. If it never asks: Settings > Privacy & security > Microphone"
}

# --------------------------------------------------------------- 6. optional
Write-Step "6/6  Optional extras (Jarvis runs without every one of these)"

if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Ok "Claude Code CLI found - sign in once by running: claude"
} else {
    Write-Warn "Claude Code CLI not installed - only the fallback hands are missing. npm install -g @anthropic-ai/claude-code"
}

if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Ok "ffmpeg found - the Aisha Uzbek voice will work"
} else {
    Write-Warn "ffmpeg not installed - only needed if you set tts.backend: aisha. winget install Gyan.FFmpeg"
}

if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Ok "Docker found - delegate_to_openclaw works once you run the gateway (docs/OPENCLAW.md)"
} else {
    Write-Warn "Docker not installed - delegate_to_openclaw stays hidden. Entirely optional."
}

Write-Warn "Telegram is opt-in: keys in .env, then .venv\Scripts\python -m jarvis --telegram-login"
Write-Warn "The Telegram bot stays off until telegram_bot.allow_from in config.yaml holds YOUR numeric id (ask @userinfobot)"

# ------------------------------------------------------------------- verdict
Write-Host ""
if ($blockers.Count -gt 0) {
    Write-Host "  Not ready yet - $($blockers.Count) thing(s) to fix:" -ForegroundColor Red
    foreach ($b in $blockers) { Write-Host "   - $b" -ForegroundColor Red }
    foreach ($n in $notes) { Write-Host "   note: $n" -ForegroundColor DarkGray }
    Write-Host "`n  Fix those and run this script again." -ForegroundColor Red
    Pop-Location
    exit 1
}

Write-Host "  Prerequisites are in place." -ForegroundColor Green

if ($SkipDoctor -or -not (Test-Path $venvPython)) {
    Write-Host "  Next:  .venv\Scripts\python -m jarvis --doctor" -ForegroundColor White
    Pop-Location
    exit 0
}

Write-Step "Running --doctor (microphone, wake word, Gemini connection)"
& $venvPython -m jarvis --doctor
$doctor = $LASTEXITCODE

Write-Host ""
if ($doctor -eq 0) {
    Write-Host "  Ready. Start it with:" -ForegroundColor Green
    Write-Host "    .venv\Scripts\python -m jarvis" -ForegroundColor White
    Write-Host "  Or try it without a microphone first:" -ForegroundColor DarkGray
    Write-Host "    .venv\Scripts\python -m jarvis --text `"papkamda qanday fayllar bor?`"" -ForegroundColor White
    Write-Host ""
    Write-Host "  One thing to know before you talk to it: config.yaml ships with" -ForegroundColor Yellow
    Write-Host "  agent.autonomy: `"guarded`", so destructive commands ask first." -ForegroundColor Yellow
    Write-Host "  Flip it to `"full`" once you trust what it hears - then nothing asks." -ForegroundColor Yellow
} else {
    Write-Host "  --doctor found problems above (the red XATO rows). Fix those, then run:" -ForegroundColor Red
    Write-Host "    .venv\Scripts\python -m jarvis --doctor" -ForegroundColor White
}
Pop-Location
exit $doctor
