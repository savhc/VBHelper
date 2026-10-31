@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 必须用 D:\python（3.14.6 + PyInstaller 6.21），裸 python 可能解析到别的版本
set PYTHON=D:\python\python.exe
if not exist "%PYTHON%" set PYTHON=python

rem 用 spec 构建：hiddenimports、资源清单、图标都写在 VBHelper.spec 里。
rem 不要再加 --name/--add-data/--icon 等参数，也不要加 --specpath：
rem 那样会重新生成 spec，把 hiddenimports 覆盖掉，features 下的功能会在
rem EXE 里静默消失（表现为菜单缺项）。
"%PYTHON%" -m PyInstaller --clean --distpath dist --workpath build VBHelper.spec

echo.
echo 打包完成: %cd%\dist\VBHelper.exe
pause
