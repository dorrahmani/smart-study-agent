@echo off
:: setup_auto_auth.bat — registers a Task Scheduler task that runs auto_refresh_notebooklm.py every 4 hours.
:: Run once from PowerShell (as a regular user, not Admin):
::   .\setup_auto_auth.bat

setlocal enabledelayedexpansion

set TASK_NAME=SmartStudy_NotebookLM_AuthRefresh
set SCRIPT_DIR=%~dp0
set PYTHON_SCRIPT=%SCRIPT_DIR%auto_refresh_notebooklm.py

:: Find python.exe
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] python לא נמצא ב-PATH
    exit /b 1
)
for /f "delims=" %%i in ('where python') do (
    set PYTHON_EXE=%%i
    goto :found_python
)
:found_python

echo Python: %PYTHON_EXE%
echo Script: %PYTHON_SCRIPT%
echo Task:   %TASK_NAME%
echo.

:: Delete an existing task (if any) and do not silence errors
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: Create the task:
::   - every 4 hours, after the user logs in
::   - RepeatInterval PT4H (every 4 hours)
::   - Duration P1D  (runs 24 hours a day)
::   - RunLevel HIGHEST = run with the user's full permissions (not admin)
schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "\"%PYTHON_EXE%\" \"%PYTHON_SCRIPT%\"" ^
    /sc HOURLY ^
    /mo 4 ^
    /f

if %errorlevel% neq 0 (
    echo [ERROR] יצירת המשימה נכשלה.
    exit /b 1
)

echo.
echo [OK] המשימה נרשמה בהצלחה!
echo      תרוץ כל 4 שעות כשהמחשב מופעל.
echo      לוג: %SCRIPT_DIR%logs\notebooklm_auth_refresh.log
echo.
echo כדי לבדוק:  schtasks /query /tn "%TASK_NAME%"
echo כדי להריץ עכשיו:  schtasks /run /tn "%TASK_NAME%"
echo כדי למחוק:  schtasks /delete /tn "%TASK_NAME%" /f
echo.
pause
