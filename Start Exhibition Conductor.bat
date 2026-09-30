@echo off
rem E-paper Exhibition Conductor - double-click to start the EXHIBITION's
rem Conductor on this PC. This is the exhibition's authoring / Upload / Send
rem Conductor: the same program as Start Conductor.bat, but a separate
rem application - its own window title, its own port (8766, the production
rem show's is 8765), its own data folder (exhibition-data\, the show's is
rem showdata\) and an amber EXHIBITION badge on the page - so the two can
rem never be mixed up. Nothing you do here touches the production show.
rem
rem Its job on this PC: build the exhibition's show (CSV, timeline, music),
rem Upload it to the units for a rehearsal on the router, and send the
rem workspace to radxa-05 (Units tab, "Send workspace to ..."). At the venue
rem radxa-05 runs its OWN Conductor (radxa/EXHIBITION.md) - close this one
rem there. Never run this and Start Conductor.bat against the fleet at the
rem same time: two Conductors "correct" each other's T0 and fight.
rem
rem It serves http://127.0.0.1:8766 (this PC only) and opens it in the
rem default browser. Keep this window open while you work; close it (or
rem press Ctrl+C) to stop the UI. Starting it twice just opens the page again.
title E-paper Exhibition Conductor
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

%PY% -m conductor serve --workspace exhibition-data --port 8766 --label EXHIBITION --open
if errorlevel 1 pause
