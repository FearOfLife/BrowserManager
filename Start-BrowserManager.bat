@echo off
cd /d "%~dp0"
set "JAVAFX_HOME=C:\javafx-sdk-26"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-javafx.ps1" -JavaFxHome "%JAVAFX_HOME%" -Detached
if errorlevel 1 pause
