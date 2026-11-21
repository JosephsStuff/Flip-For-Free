@echo off
cd /d "%~dp0"
set FLIP_LAUNCHER=1
python -m pip install -q -r requirements.txt
:start
python app.py %*
rem exit code 3 = restart after an update
if %errorlevel%==3 goto start
pause
