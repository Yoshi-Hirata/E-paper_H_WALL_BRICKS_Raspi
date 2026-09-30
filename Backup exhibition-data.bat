@echo off
rem E-paper Exhibition Conductor - back up the EXHIBITION's data
rem (exhibition-data\) to a dated zip next to this repository folder, e.g.
rem ..\exhibition-data-20260929-1830.zip. The production show's data
rem (showdata\) is a different folder with its own Backup showdata.bat.
rem The zip holds everything the exhibition UI keeps: CSV files, the timeline
rem and unit assignments (show.json), the music file, the undo history and
rem this PC's fleet.json. Unzip it into a fresh clone on another PC (so that
rem exhibition-data\files\... exists there) and Start Exhibition Conductor.bat
rem shows the same show.
title Backup exhibition-data
cd /d "%~dp0"
if not exist "exhibition-data\" (
  echo No exhibition-data folder here yet - nothing to back up.
  pause
  exit /b 1
)
for /f "tokens=1-5 delims=/:. " %%a in ("%date% %time%") do set STAMP=%%a%%b%%c-%%d%%e
set STAMP=%STAMP: =0%
set OUT=..\exhibition-data-%STAMP%.zip
powershell -NoProfile -Command "Compress-Archive -Path 'exhibition-data' -DestinationPath '%OUT%' -Force"
if errorlevel 1 (
  echo Backup failed.
  pause
  exit /b 1
)
for %%F in ("%OUT%") do echo Saved %%~fF (%%~zF bytes)
pause
