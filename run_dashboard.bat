@echo off
chcp 65001 > nul
title Smart Study Dashboard
cd /d "%~dp0"
start "" http://localhost:8501
C:\Python314\python.exe -m streamlit run dashboard/app.py
