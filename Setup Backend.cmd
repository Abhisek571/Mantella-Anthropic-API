@echo off
cd /d "%~dp0"
py -3.11 --version >nul 2>&1
if errorlevel 1 goto nopython
py -3.11 setup_backend.py
set "RESULT=%ERRORLEVEL%"
goto done
:nopython
echo Python 3.11 was not found. Install Python 3.11.9 from python.org with its Python launcher,
echo then run this file again: https://www.python.org/downloads/release/python-3119/
set "RESULT=1"
:done
echo.
if not "%RESULT%"=="0" echo Setup failed. See the error above and INSTALL.txt.
pause
exit /b %RESULT%
