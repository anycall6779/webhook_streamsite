@echo off
chcp 65001 >nul 2>&1
title 🤖 Streaming Bot Launcher
cd /d "%~dp0"

echo ============================================
echo   Streaming Bot Launcher
echo ============================================
echo.

:: Python 설치 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않거나 PATH에 등록되지 않았습니다.
    pause
    exit /b 1
)

:: 치지직 봇 실행
echo [1/2] 치지직 봇 시작 중...
start "Chzzk Bot" /min python chzz.py
echo       → 치지직 봇 실행 완료

:: 충돌 방지 대기
timeout /t 2 /nobreak >nul

:: 유튜브 봇 실행
echo [2/2] 유튜브 봇 시작 중...
start "Youtube Bot" /min python youtube.py
echo       → 유튜브 봇 실행 완료

echo.
echo ============================================
echo   모든 봇이 실행되었습니다!
echo   각 봇은 별도 창에서 최소화 상태로 실행 중입니다.
echo   이 창을 닫아도 봇은 계속 동작합니다.
echo ============================================
echo.
pause