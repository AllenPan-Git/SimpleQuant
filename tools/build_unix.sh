#!/bin/sh
# macOS / Linux 打包（GitHub Actions 里也用它）：PyInstaller → 发布用的压缩包
#   sh tools/build_unix.sh           用项目的 .venv；环境变量 PYTHON 可指定别的 Python
# 输出（<平台> 如 macos-arm64、linux-x86_64，见 simplequant/system.py 的 target）：
#   macOS：dist/macos/SimpleQuant.app、SimpleQuant-<版本>-<平台>.dmg（给用户下载）、.tar.gz（自动更新用）
#   Linux：dist/linux/SimpleQuant/、SimpleQuant-<版本>-<平台>.tar.gz（给用户下载，自动更新也用它）
set -e
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
VER=$("$PY" -c "import simplequant; print(simplequant.__version__)")
SYS=$("$PY" -c "from simplequant.system import SYSTEM; print(SYSTEM)")
TARGET=$("$PY" -c "from simplequant.system import TARGET; print(TARGET)")
OUT="dist/$SYS"
NAME="SimpleQuant-$VER-$TARGET"

# 固定哈希种子和时间戳：同样的代码两次打包得到相同的文件，自动更新的补丁才小（与 tools/release.py 一致）
export PYTHONHASHSEED=0 SOURCE_DATE_EPOCH=1735689600
"$PY" -m PyInstaller SimpleQuant.spec --noconfirm --clean --distpath "$OUT" --workpath "build/$SYS"
rm -f "$OUT"/*.tar.gz "$OUT"/*.dmg

if [ "$SYS" = "macos" ]; then
    rm -rf "$OUT/SimpleQuant"                      # BUNDLE 之前的单文件夹版本，.app 里已经有了
    # PyInstaller 已做 ad-hoc 签名（Apple 芯片上必须有）；检查一遍，签名不完整的 .app 打不开
    codesign --verify --deep --strict "$OUT/SimpleQuant.app"
    COPYFILE_DISABLE=1 tar -czf "$OUT/$NAME.tar.gz" -C "$OUT" SimpleQuant.app
    STAGE="build/$SYS/dmg"
    rm -rf "$STAGE"
    mkdir -p "$STAGE"
    cp -R "$OUT/SimpleQuant.app" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"      # 打开 dmg 后把程序拖到「应用程序」
    hdiutil create -volname SimpleQuant -srcfolder "$STAGE" -ov -format UDZO "$OUT/$NAME.dmg"
    echo "完成：$OUT/SimpleQuant.app、$OUT/$NAME.dmg"
else
    tar -czf "$OUT/$NAME.tar.gz" -C "$OUT" SimpleQuant
    echo "完成：$OUT/SimpleQuant/SimpleQuant、$OUT/$NAME.tar.gz"
fi
