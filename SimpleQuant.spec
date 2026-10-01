# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置：双击 build.bat，或 .venv\Scripts\pyinstaller SimpleQuant.spec --noconfirm
输出 dist\SimpleQuant\（整个文件夹一起分发，运行其中的 SimpleQuant.exe）

- 单文件夹模式（onedir）：启动快，不用每次解压
- simplequant / gui / ui 只以 .py 源码形式放在 _internal 里（不进 exe 内的 PYZ）：
  导出 Python 脚本时用 inspect.getsource 读取策略基类等源码；自动更新时只改了自己代码的版本只需下载这几个 .py，
  exe 只在依赖库变化时才变（PYZ 在 exe 里，约 40 MB）。打包时要固定 PYTHONHASHSEED、SOURCE_DATE_EPOCH（build.bat、
  tools/release.py 已设置），否则 exe 里的时间戳、base_library.zip 里的文件顺序每次不同
- 不打包：tests、tools 等（只收集程序实际 import 的模块）；排除 streamlit、pytest、LiteLLM（体积太大，界面里会提示改用 OpenAI 兼容接口）、torch 等无关的大库
"""

import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# 记录源码目录：打包版第一次运行时据此找到旧数据（simplequant/migrate.py）
(ROOT / "simplequant" / "_build_info.json").write_text(
    json.dumps({"source_dir": str(ROOT)}, ensure_ascii=False), encoding="utf-8")

datas = [
    (str(ROOT / "gui" / "static"), "gui/static"),
    (str(ROOT / "simplequant" / "_build_info.json"), "simplequant"),
    (str(ROOT / "simplequant" / "update" / "apply_update.ps1"), "simplequant/update"),   # 自动更新替换文件的脚本
]
for pkg in ("nicegui", "akshare", "mootdx", "plotly", "baostock"):
    datas += collect_data_files(pkg)

# 新浪行情解码用的 V8 引擎（akshare → py_mini_racer）；打包后它在 _MEIPASS 根目录找 mini_racer.dll
import py_mini_racer
binaries = [(str(Path(py_mini_racer.__file__).with_name("mini_racer.dll")), ".")]

hiddenimports = (
    collect_submodules("simplequant") + collect_submodules("gui") + ["ui.shared", "ui.charts", "ui.texts"]
    + collect_submodules("nicegui") + ["webview", "anthropic", "openai", "pyarrow", "matplotlib.backends.backend_agg"]
)

excludes = ["streamlit", "pytest", "_pytest", "litellm", "torch", "torchvision", "tensorflow", "IPython", "jupyter",
            "notebook", "jupyter_client", "ipykernel", "sklearn", "PyQt5", "PyQt6", "PySide2", "PySide6",
            "boto3", "botocore"]   # anthropic 的 AWS Bedrock 支持用不到

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    module_collection_mode={"simplequant": "py", "gui": "py", "ui": "py"},
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SimpleQuant",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "gui" / "static" / "icon.ico") if (ROOT / "gui" / "static" / "icon.ico").exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SimpleQuant")
