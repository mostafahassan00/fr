@echo off
rem Face Sorter - runs straight from the USB stick, nothing to install.
cd /d "%~dp0"
start "" "%~dp0python\pythonw.exe" "%~dp0app\face_sorter.py"
