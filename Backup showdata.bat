@echo off
rem E-paper Show Conductor - back up the show data (showdata\) to a dated zip
rem next to this repository folder, e.g. ..\showdata-20260929-1830.zip.
rem The zip holds everything the UI keeps: CSV files, the timeline and unit
rem assignments (show.json), the music file and the undo history. Unzip it
rem into a fresh clone on another PC (so that showdata\files\... exists
rem there) and Start Conductor.bat shows the same show.
title Backup showdata
cd /d "%~dp0"
if not exist "showdata\" (
  echo No showdata folder here yet - nothing to back up.
  pause
  exit /b 1
)
for /f "tokens=1-5 delims=/:. " %%a in ("%date% %time%") do set STAMP=%%a%%b%%c-%%d%%e
set STAMP=%STAMP: =0%
set OUT=..\showdata-%STAMP%.zip
powershell -NoProfile -Command "Compress-Archive -Path 'showdata' -DestinationPath '%OUT%' -Force"
if errorlevel 1 (
  echo Backup failed.
  pause
  exit /b 1
)
for %%F in ("%OUT%") do echo Saved %%~fF (%%~zF bytes)
pause
