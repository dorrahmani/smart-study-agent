@echo off
chcp 65001 > nul
title Smart Study Agent
cd /d "%~dp0"

echo.
echo  ============================================
echo    Smart Study Agent - מתחיל...
echo  ============================================
echo.

C:\Python314\python.exe scripts\run_now.py

echo.
echo  ============================================
echo    סיום! אפשר לסגור את החלון.
echo  ============================================
echo.
pause
