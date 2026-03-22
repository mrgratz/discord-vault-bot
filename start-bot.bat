@echo off
REM Discord Vault Bot — auto-start launcher (no console window).
REM Copy this file to %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
REM to start the bot automatically on Windows login.

cd /d "%~dp0"
start /b pythonw bot.py
