@echo off
setlocal
cd /d "%~dp0"
title Build DeadlockBlocker.exe

if not exist deadlock_block.py (
    echo deadlock_block.py must be in the same folder as this file.
    pause & exit /b 1
)

set PY=py
where py >nul 2>nul || set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install it from python.org and tick "Add Python to PATH".
    pause & exit /b 1
)

set DATA=
if exist sdr_config.json (
    echo Found sdr_config.json, it will be packed into the exe.
    set DATA=--add-data "sdr_config.json;."
) else (
    echo No sdr_config.json yet. Run deadlock_block.py once with proxy/VPN on to create it, then build again.
)

echo [1/3] Installing PySide6 and PyInstaller...
%PY% -m pip install --upgrade pyside6 pyinstaller
if errorlevel 1 ( echo pip failed. & pause & exit /b 1 )

echo [2/3] Building exe, this takes a few minutes...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --uac-admin %DATA% --name DeadlockBlocker deadlock_block.py
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )

echo [3/3] Cleaning up...
copy /y dist\DeadlockBlocker.exe DeadlockBlocker.exe >nul
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q DeadlockBlocker.spec 2>nul

echo.
echo Done: DeadlockBlocker.exe is next to this file. Attach it to the GitHub release.
pause
