@echo off
if "%~1"=="--elevated" goto :run

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
    exit /b
)

:run
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3.13 -V >nul 2>&1
    if %errorlevel% equ 0 (
        py -3.13 gamepad_macro.py
    ) else (
        py -3 gamepad_macro.py
    )
) else (
    python gamepad_macro.py
)
if errorlevel 1 pause
