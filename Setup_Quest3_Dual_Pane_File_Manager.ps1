# Quest 3 Dual-Pane File Manager - first-run setup and launcher.
# Invoked by Start_Quest3_Dual_Pane_File_Manager.cmd (Windows PowerShell 5.1+).
# Installs Android SDK Platform-Tools beside the launcher; installs a PRIVATE
# Python runtime with Tkinter only if no suitable Python is already available.

# Get the application folder directly from the script path ($PSScriptRoot).
# Do not accept a directory argument from CMD: a path ending in backslash can
# be misquoted by Windows native argument parsing and produce invalid characters.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Require-File([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description was not found at '$Path'. Re-extract the complete ZIP."
    }
}

function Download-OfficialFile([string]$Url, [string]$Destination) {
    # The only sources are the official Google and Python distribution domains.
    if (-not ($Url.StartsWith('https://dl.google.com/android/repository/') -or
              $Url.StartsWith('https://www.python.org/ftp/python/'))) {
        throw "Refusing unexpected download source: $Url"
    }
    Write-Host "Downloading $Url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf) -or
        (Get-Item -LiteralPath $Destination).Length -lt 100000) {
        throw "Download was empty or incomplete: $Url"
    }
}

function Test-ADB([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    try {
        & $Executable version 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Install-ADB([string]$RootDirectory) {
    $adbExe = Join-Path $RootDirectory 'platform-tools\adb.exe'
    if (Test-ADB $adbExe) {
        Write-Host '[OK] ADB already installed in .\platform-tools'
        return
    }

    Write-Host '[SETUP] Installing official Google Android SDK Platform-Tools locally...'
    $workDir = Join-Path $RootDirectory ('.quest-setup-' + [Guid]::NewGuid().ToString('N'))
    $zipPath = Join-Path $workDir 'platform-tools.zip'
    $extractPath = Join-Path $workDir 'unpacked'
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    try {
        Download-OfficialFile 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' $zipPath
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
        $source = Join-Path $extractPath 'platform-tools'
        Require-File (Join-Path $source 'adb.exe') 'Google Platform-Tools adb.exe'
        $destination = Join-Path $RootDirectory 'platform-tools'
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        # Copy all companion DLLs as well as adb.exe; an isolated adb.exe is insufficient.
        Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $destination -Recurse -Force
        if (-not (Test-ADB $adbExe)) {
            throw 'ADB installed but failed the version check. Check antivirus or permissions.'
        }
        Write-Host '[OK] ADB installed in .\platform-tools'
    } finally {
        Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-Python([string]$Executable, [string[]]$PrefixArguments) {
    try {
        $argsToRun = @($PrefixArguments) + @(
            '-c',
            'import sys, tkinter; assert sys.version_info >= (3, 9); assert sys.version_info < (4, 0)'
        )
        & $Executable @argsToRun 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python([string]$RootDirectory) {
    # Prefer local installs and the old Quest app's virtual environment.
    $localPython = Join-Path $RootDirectory 'python-runtime\python.exe'
    if (Test-Python $localPython @()) {
        return [pscustomobject]@{ Exe = $localPython; Args = @(); Label = 'local python-runtime' }
    }
    $venvPython = Join-Path $RootDirectory '.venv\Scripts\python.exe'
    if (Test-Python $venvPython @()) {
        return [pscustomobject]@{ Exe = $venvPython; Args = @(); Label = 'existing .venv' }
    }
    # Prefer the Python launcher over PATH entries, which may be Microsoft Store aliases.
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $candidate) { continue }
        $prefix = @()
        if ($name -eq 'py.exe') { $prefix = @('-3') }
        if (Test-Python $candidate.Source $prefix) {
            return [pscustomobject]@{ Exe = $candidate.Source; Args = $prefix; Label = "existing $name" }
        }
    }
    return $null
}

function Install-Python([string]$RootDirectory) {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'Automatic Python installation requires 64-bit Windows 10/11.'
    }
    # A dated, official release URL avoids trusting redirects from third-party sources.
    # Full installer is REQUIRED: the embeddable Python ZIP does not contain Tkinter/Tcl/Tk.
    $version = '3.13.14'
    $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    $runtimeDir = Join-Path $RootDirectory 'python-runtime'
    $installerPath = Join-Path $RootDirectory ('.quest-python-' + $version + '-installer.exe')
    Write-Host '[SETUP] No usable Python 3.9+ with Tkinter found.'
    Write-Host '[SETUP] Installing private Python with Tkinter into .\python-runtime...'
    try {
        Download-OfficialFile $url $installerPath
        $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
        if ($signature.Status -ne 'Valid' -or
            $null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw 'Python installer signature verification failed; refusing to execute the download.'
        }
        # No administrator rights or system PATH modifications. Include Tkinter and pip.
        $installerArguments = '/quiet InstallAllUsers=0 Include_tcltk=1 Include_pip=1 Include_test=0 Include_doc=0 PrependPath=0 AssociateFiles=0 Include_launcher=0 TargetDir="' + $runtimeDir + '"'
        $process = Start-Process -FilePath $installerPath -ArgumentList $installerArguments -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Python installer failed (exit code $($process.ExitCode))."
        }
        $pythonExe = Join-Path $runtimeDir 'python.exe'
        if (-not (Test-Python $pythonExe @())) {
            throw 'Python installation finished but Python/Tkinter validation failed.'
        }
        Write-Host '[OK] Private Python with Tkinter installed in .\python-runtime'
        return [pscustomobject]@{ Exe = $pythonExe; Args = @(); Label = 'local python-runtime' }
    } finally {
        Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
    }
}

try {
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        throw 'Cannot locate the setup script folder. Run Start_Quest3_Dual_Pane_File_Manager.cmd.'
    }
    $root = (Get-Item -LiteralPath $PSScriptRoot -ErrorAction Stop).FullName
    $scriptPath = Join-Path $root 'Quest3_Dual_Pane_File_Manager.py'
    Require-File $scriptPath 'Python application'
    Set-Location -LiteralPath $root
    Write-Host '============================================================='
    Write-Host '    QUEST 3 DUAL-PANE FILE MANAGER - AUTO SETUP / LAUNCH'
    Write-Host '============================================================='
    Write-Host "Application folder: $root"
    Write-Host ''

    Install-ADB $root
    $python = Find-Python $root
    if ($null -eq $python) { $python = Install-Python $root }
    Write-Host "[OK] Python: $($python.Label)"
    Write-Host '[OK] No third-party pip libraries required (standard-library Tkinter app).'
    Write-Host ''
    Write-Host 'Launching Quest 3 File Manager...'
    Write-Host ''
    $launchArgs = @($python.Args) + @($scriptPath)
    & $python.Exe @launchArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Quest 3 File Manager exited with code $LASTEXITCODE."
    }
    exit 0
} catch {
    Write-Host ''
    Write-Host ('SETUP ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber -gt 0) {
        Write-Host ("Setup script line: " + $_.InvocationInfo.ScriptLineNumber) -ForegroundColor Yellow
    }
    Write-Host 'If this persists, copy the error and setup script line number.'
    exit 1
}
