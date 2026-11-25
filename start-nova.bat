@echo off
rem Starts NOVA: backend (http://127.0.0.1:8000) and frontend (http://localhost:5173) in their own windows,
rem then opens the dashboard. Close the two windows to stop NOVA.
setlocal
set "ROOT=%~dp0"

if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo Python environment not found. See README.md, "Setup".
  pause
  exit /b 1
)
if not exist "%ROOT%frontend\node_modules" (
  echo Installing frontend dependencies...
  pushd "%ROOT%frontend" && call npm install && popd
)

start "NOVA backend" /D "%ROOT%backend" cmd /k "..\.venv\Scripts\python.exe run.py"
start "NOVA frontend" /D "%ROOT%frontend" cmd /k "npm run dev"

echo Waiting for NOVA to start...
timeout /t 8 /nobreak >nul
start "" http://localhost:5173
