@echo off
cd /d "%~dp0"
set FLIP_LAUNCHER=1
echo.
echo   Getting Flip for Free ready (the first time can take a minute)...
python -m pip install -q -r requirements.txt
:start
python app.py --lan %*
rem exit code 3 = restart after an update
if %errorlevel%==3 goto start
pause
