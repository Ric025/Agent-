@echo off
chcp 65001 >nul
rem 启动本机办公 Agent（Windows）。双击就行。
rem 用法：start.bat [--port 8765] [--home 数据目录] [--mock] [--no-open]
rem   第一次运行（或者依赖清单有变化）时，先自动检查这台电脑的环境、装好 Python 和依赖（setup\install.ps1），再启动。
rem   --setup     不管装没装过，重新检查、重新安装，然后启动
rem   --no-setup  跳过检查和安装，直接用现有的环境启动
rem   --check     只检查环境，告诉你哪些功能能用、哪里有问题，不启动
setlocal
cd /d "%~dp0"

set "SETUP=auto"
set "ARGS="
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--setup" (
  set "SETUP=force"
) else if /i "%~1"=="--no-setup" (
  set "SETUP=skip"
) else if /i "%~1"=="--check" (
  set "SETUP=check"
) else (
  call set ARGS=%%ARGS%% %1
)
shift
goto parse
:parsed

set "PS=powershell -NoProfile -ExecutionPolicy Bypass -File setup\install.ps1"
if "%SETUP%"=="check" (
  %PS% -Check
  pause
  exit /b
)
rem 检查环境、需要时安装。装不上也不拦着：下面退回到电脑上已有的 Python，能用的功能照常用
if "%SETUP%"=="force" %PS% -Force
if "%SETUP%"=="auto" %PS% -IfNeeded

rem 选 Python：.venv 里必需的包都在就用它（这次没装成、但以前装好的也算）；否则用电脑上已有的
set "PY="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('openpyxl', 'langchain', 'langgraph')) else 1)" >nul 2>nul && set "PY=.venv\Scripts\python.exe"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto nopython

if not "%PY%"==".venv\Scripts\python.exe" (
  echo.
  echo 提示：没有用上自动装好的环境，现在用的是电脑上的 Python。
  echo       新内核、操作网页这些功能可能用不了（打开后在 设置 和 /状态 里能看到原因）。修好网络后：start.bat --setup
  echo.
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
%PY% -m agent %ARGS%
if errorlevel 1 pause
endlocal
exit /b

:nopython
echo.
echo 没有能用的 Python，程序起不来。
echo   · 先检查网络，再运行 start.bat --setup 重新自动安装；
echo   · 或者从 https://www.python.org/downloads/ 装一个 Python 3.12（安装时勾选 Add python.exe to PATH），再双击 start.bat。
echo   安装记录在 setup\安装记录.txt。
pause
exit /b 1
