@echo off
cd /d "%~dp0"
echo.
echo ==========================================
echo   JLPT Japanese Vocabulary Local Web App
echo ==========================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  set PY=python
)
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Python package installation failed.
  pause
  exit /b 1
)
echo.
echo Open: http://127.0.0.1:8000
echo Press Ctrl+C to stop the server.
echo.
start "" http://127.0.0.1:8000
%PY% -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
