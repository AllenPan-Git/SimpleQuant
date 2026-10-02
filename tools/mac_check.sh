#!/bin/sh
# SimpleQuant 的 macOS 检查脚本：汇总系统、签名、日志、定时任务等信息到桌面上的文本文件，便于排查问题
#   sh mac_check.sh          生成 ~/Desktop/SimpleQuant-检查结果.txt
#   sh mac_check.sh clean    删除程序、数据目录与定时任务（在借用的电脑上测试完毕后使用）
# 也可以不下载直接运行：
#   curl -fsSL https://raw.githubusercontent.com/AllenPan-Git/SimpleQuant/main/tools/mac_check.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/AllenPan-Git/SimpleQuant/main/tools/mac_check.sh | sh -s clean

APP="/Applications/SimpleQuant.app"
for p in "$HOME/Applications/SimpleQuant.app" "$HOME/Desktop/SimpleQuant.app" "$HOME/Downloads/SimpleQuant.app"; do
    [ ! -d "$APP" ] && [ -d "$p" ] && APP="$p"     # 没有管理员权限时可能放在这些位置
done
DATA="$HOME/Library/Application Support/SimpleQuant"
PLIST="$HOME/Library/LaunchAgents/com.simplequant.paperdaily.plist"

if [ "$1" = "clean" ]; then
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    pkill -f "$APP/Contents/MacOS/SimpleQuant" 2>/dev/null
    rm -rf "$APP" "$DATA" "$APP.update-backup"
    echo "已删除：SimpleQuant 程序、数据目录、定时任务。"
    echo "下载的 dmg / zip 文件请在「下载」文件夹中自行删除。"
    exit 0
fi

OUT="$HOME/Desktop/SimpleQuant-检查结果.txt"
section() { printf '\n===== %s =====\n' "$1"; }
{
    echo "SimpleQuant 检查结果  $(date '+%Y-%m-%d %H:%M:%S')"
    section "系统"
    sw_vers
    echo "芯片：$(uname -m)  $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
    echo "Rosetta 转译中：$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)"

    section "程序"
    if [ -d "$APP" ]; then
        echo "版本：$(defaults read "$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null)"
        echo "可执行文件架构：$(file "$APP/Contents/MacOS/SimpleQuant" | sed 's/.*: //')"
        echo "--- codesign -dv"
        codesign -dv "$APP" 2>&1 | grep -E "Identifier|Format|Signature|TeamIdentifier"
        echo "--- codesign --verify --deep --strict"
        codesign --verify --deep --strict "$APP" 2>&1 && echo "签名完整"
        echo "--- spctl（Gatekeeper，未公证时显示 rejected 属正常）"
        spctl --assess --type execute -v "$APP" 2>&1
        echo "--- 隔离标记（com.apple.quarantine）"
        xattr -p com.apple.quarantine "$APP" 2>/dev/null || echo "无"
        echo "--- 正在运行的进程"
        ps -axo pid,etime,comm | grep -F "$APP" | grep -v grep || echo "无"
    else
        echo "未找到 $APP"
    fi

    section "数据目录 $DATA"
    if [ -d "$DATA" ]; then
        du -sh "$DATA" 2>/dev/null
        ls -la "$DATA" "$DATA/data_cache" 2>/dev/null
    else
        echo "不存在（程序尚未运行过）"
    fi

    section "程序日志 logs/app.log（最后 150 行）"
    tail -150 "$DATA/logs/app.log" 2>/dev/null || echo "无"

    section "模拟盘日志 daily.log（最后 20 行）"
    tail -20 "$DATA/data_cache/paper/daily.log" 2>/dev/null || echo "无"

    section "定时任务（launchd）"
    if [ -f "$PLIST" ]; then
        plutil -p "$PLIST"
        launchctl print "gui/$(id -u)/com.simplequant.paperdaily" 2>&1 | grep -E "state|last exit|runs|program" | head -10
    else
        echo "未设置"
    fi

    section "自动更新"
    ls -la "$DATA/updates" 2>/dev/null || echo "无"
    tail -30 "$DATA/updates/update.log" 2>/dev/null
    cat "$DATA/updates/result.json" 2>/dev/null

    section "崩溃报告（最近 3 个）"
    ls -t "$HOME/Library/Logs/DiagnosticReports/" 2>/dev/null | grep -i simplequant | head -3 || echo "无"
} > "$OUT" 2>&1

echo "已生成：$OUT"
echo "请将该文件带回（U 盘、邮件发给自己等均可）。"
