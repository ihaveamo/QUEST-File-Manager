@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Quest 3 - Dual-Pane File Manager

rem Always use the folder containing THIS CMD, not the folder the user launched it from.
set "APP_DIR=%~dp0"
set "BOOTSTRAP=%APP_DIR%Setup_Quest3_Dual_Pane_File_Manager.ps1"
set "APP=%APP_DIR%Quest3_Dual_Pane_File_Manager.py"

if not exist "%APP%" (
    echo ERROR: Missing Quest3_Dual_Pane_File_Manager.py
    echo Please extract ALL files from the application ZIP into the same folder.
    pause
    exit /b 1
)
if not exist "%BOOTSTRAP%" (
    echo ERROR: Missing Setup_Quest3_Dual_Pane_File_Manager.ps1
    echo Please extract ALL files from the application ZIP into the same folder.
    pause
    exit /b 1
)

where powershell.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: Windows PowerShell is required for automatic setup.
    pause
    exit /b 1
)

rem The setup script reads its own location via $PSScriptRoot. Do not pass APP_DIR:
rem %%~dp0 always ends in a backslash, which can escape the closing quote in a
rem native Windows command line and introduce an illegal quote into the path.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP%"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo Setup or the application failed with exit code %RESULT%.
    echo Read the error above. Nothing should need to be installed manually.
    pause
)
exit /b %RESULT%
