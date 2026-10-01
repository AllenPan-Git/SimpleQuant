@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 打包：1) PyInstaller 生成 dist\SimpleQuant\（整个文件夹，运行其中的 SimpleQuant.exe）
rem       2) 装了 Inno Setup 6 时再生成安装包 dist\SimpleQuant-<版本>-Setup.exe（发给别人用这个）
rem 发布新版本用 tools\release.py（打包 + 更新清单 + 上传 GitHub），这个文件只打包
rem 固定哈希种子和时间戳：同样的代码两次打包得到相同的 exe，自动更新的补丁才小（与 tools\release.py 一致）
set PYTHONHASHSEED=0
set SOURCE_DATE_EPOCH=1735689600
rem 图标：gui\static\icon.ico，由 tools\make_icon.py 生成；版本号在 simplequant\__init__.py
".venv\Scripts\python.exe" -m PyInstaller SimpleQuant.spec --noconfirm --clean
if errorlevel 1 (
    echo 打包失败，见上方输出
    pause
    exit /b 1
)
echo.
echo 完成：dist\SimpleQuant\SimpleQuant.exe

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo 未安装 Inno Setup 6，跳过安装包（安装：winget install JRSoftware.InnoSetup）
    pause
    exit /b 0
)
for /f %%v in ('".venv\Scripts\python.exe" -c "import simplequant; print(simplequant.__version__)"') do set VER=%%v
"%ISCC%" /Q /DAppVersion=%VER% installer\SimpleQuant.iss
if errorlevel 1 (
    echo 安装包生成失败，见上方输出
    pause
    exit /b 1
)
echo 完成：dist\SimpleQuant-%VER%-Setup.exe
pause
