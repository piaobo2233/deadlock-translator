@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "LOCKLINGO_VERSION=%~1"
if not defined LOCKLINGO_VERSION (
  echo 用法：scripts\build.bat 版本号
  echo 示例：scripts\build.bat 1.0.0-test.4-rc.2
  exit /b 2
)
echo ==================================================
echo   锁语 LockLingo - 内置 OCR 与当前用户安装器
 echo ==================================================
python -X utf8 scripts\ocr_runtime.py verify
if errorlevel 1 goto failed
python -X utf8 scripts\build_portable.py --version "%LOCKLINGO_VERSION%"
if errorlevel 1 goto failed
python -X utf8 scripts\build_installer.py --archive "dist\LockLingo-%LOCKLINGO_VERSION%-windows-x64.zip" --version "%LOCKLINGO_VERSION%"
if errorlevel 1 goto failed
echo [完成] dist\LockLingo-%LOCKLINGO_VERSION%-setup.exe
if not defined CI pause
exit /b 0
:failed
set "LOCKLINGO_BUILD_EXIT=%errorlevel%"
echo [失败] 构建未完成，错误码 %LOCKLINGO_BUILD_EXIT%。请检查上方信息与 .temp\build-evidence 日志。
if not defined CI pause
exit /b %LOCKLINGO_BUILD_EXIT%
