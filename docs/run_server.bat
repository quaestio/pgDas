@echo off
title PGDAS server - http://127.0.0.1:8001/
cd /d C:\django_projects\PgDas

rem Do not start a second copy: the first one would keep answering and the new one would stop with "port already in use".
netstat -ano | findstr /R /C:":8001 .*LISTENING" >nul
if %errorlevel%==0 (
  echo Something is already listening on port 8001, so PGDAS is probably running: http://127.0.0.1:8001/
  echo Close the other window first if you want to start it again.
  pause
  exit /b 1
)

echo Starting PGDAS on http://127.0.0.1:8001/  ^(Ctrl+C stops it^)
echo.
.\.venv\Scripts\python.exe manage.py runserver 8001

rem Only reached when the server stops. Keep the window open so the reason stays on screen.
echo.
echo The server stopped ^(exit code %errorlevel%^). Read the lines above for the reason.
pause
