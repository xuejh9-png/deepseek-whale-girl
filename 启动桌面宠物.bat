@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0windows"

echo.
echo   WorkBuddy 桌面宠物（Windows 11）
echo   --------------------------------
echo.

where node >nul 2>nul
if errorlevel 1 (
  echo   [!] 没找到 Node.js。
  echo.
  echo       这个版本要用 Node 18 以上才能跑。两种办法：
  echo         1^) 去 https://nodejs.org 下 LTS 版装一下，再双击本文件
  echo         2^) 不用装 Node，直接下已打好的安装包（见 README 的 Releases 一节）
  echo.
  pause
  exit /b 1
)

if not exist "node_modules\electron" (
  echo   首次运行，正在安装依赖（约 1-3 分钟，只做一次）...
  echo.
  call npm install --no-audit --no-fund
  if errorlevel 1 (
    echo.
    echo   [!] 依赖安装失败，多半是网络问题。可以换国内镜像后重试：
    echo       set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
    echo.
    pause
    exit /b 1
  )
)

echo.
echo   启动中...这个黑窗口先别关，关掉宠物就退出了。
echo   退出宠物：右键长按她，或按 Ctrl+Alt+Q。
echo.

call npm start
endlocal
