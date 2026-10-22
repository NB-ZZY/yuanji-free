@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 launch.py %*
goto done
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python launch.py %*
goto done
:missing_python
echo Install Python 3.12 from https://www.python.org/downloads/ then try again.
pause
exit /b 1
:done
if errorlevel 1 pause
