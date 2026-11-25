@echo off
rem Double-click to start the Agentaus bridge. Close the window (or Ctrl+C) to stop.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -X utf8 -m agentaus_bridge %*
pause
