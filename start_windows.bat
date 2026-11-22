@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Face Sorter

rem ---- Find Python, or install it automatically ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""

if not defined PY (
    echo Python is not installed. Installing it now, please wait...
    echo جاري تسطيب Python، استنى شوية...
    where winget >nul 2>nul
    if errorlevel 1 goto :manual
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    ) else (
        goto :manual
    )
)

rem ---- Prepare the program (first run only) ----
if not exist ".venv\Scripts\python.exe" (
    echo First run: preparing, please wait a few minutes...
    %PY% -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :error
".venv\Scripts\python.exe" face_sorter.py
exit /b 0

:manual
echo.
echo Could not install Python automatically.
echo Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this file again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:error
echo Something went wrong. Take a screenshot of this window and send it.
pause
exit /b 1
