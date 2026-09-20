@echo off
rem E-paper Show Conductor - double-click to start the show PC's web UI.
rem It serves http://127.0.0.1:8765 (this PC only) and opens it in the
rem default browser. Keep this window open during the show; close it (or
rem press Ctrl+C) to stop the UI. The units keep running a started show
rem on their own either way. Starting it twice just opens the page again.
title E-paper Show Conductor
cd /d "%~dp0"

set PY=python
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python 3.9 or newer was not found. Install it from https://www.python.org/ and try again.
    pause
    exit /b 1
  )
  set PY=py -3
)

%PY% -m conductor serve --open
if errorlevel 1 pause
