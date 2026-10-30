@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   打包 A 股 AI 量化交易系统
echo ============================================
echo.

python -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

python -m PyInstaller --noconfirm --clean --onefile --console --name AITrader ^
  --add-data "config.yaml;." ^
  --add-data ".env.example;." ^
  --exclude-module xtquant ^
  --exclude-module akshare ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module tkinter ^
  --exclude-module PIL ^
  --exclude-module pytest ^
  --exclude-module IPython ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.lifespan.on ^
  main.py
if errorlevel 1 goto :fail

echo.
echo ============================================
echo   构建完成： dist\AITrader.exe
echo ============================================
exit /b 0

:fail
echo.
echo [错误] 构建失败，请检查上方输出
exit /b 1
