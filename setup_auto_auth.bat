@echo off
:: setup_auto_auth.bat — רושם Task Scheduler להרצת auto_refresh_notebooklm.py כל 4 שעות.
:: הרץ פעם אחת מ-PowerShell (כמשתמש רגיל, לא Admin):
::   .\setup_auto_auth.bat

setlocal enabledelayedexpansion

set TASK_NAME=SmartStudy_NotebookLM_AuthRefresh
set SCRIPT_DIR=%~dp0
set PYTHON_SCRIPT=%SCRIPT_DIR%auto_refresh_notebooklm.py

:: מצא python.exe
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

:: מחק משימה קיימת (אם יש) ואל תשתק שגיאות
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: צור את המשימה:
::   - כל 4 שעות, אחרי התחברות המשתמש
::   - RepeatInterval PT4H (כל 4 שעות)
::   - Duration P1D  (פועל 24 שעות ביום)
::   - RunLevel HIGHEST = הרצה עם הרשאות משתמש מלאות (לא admin)
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
