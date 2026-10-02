#!/bin/sh
# SimpleQuant update helper for macOS / Linux. Started by simplequant/update/client.py (Updater._apply_unix)
# right before the app exits. The new program folder was already assembled and verified by the app.
#
#   apply_update.sh <pid> <macos|linux> <app dir> <new app dir> <work dir> <version>
#
# 1. Wait until the app (and anything else running from the app folder) has exited; give up after 90 s.
# 2. Rename the old folder to "<app>.update-backup" (same parent, so this is instant), move the new one in.
#    Any error moves the old folder back, so the old version stays.
# 3. Write result.json (read by the app on its next start) and start SimpleQuant again.

PID="$1"; SYSTEM="$2"; APP="$3"; NEW="$4"; WORK="$5"; VERSION="$6"
LOG="$WORK/update.log"
RESULT="$WORK/result.json"
BACKUP="$APP.update-backup"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG" 2>/dev/null; }

result() {   # result <true|false> <message>
    log "result ok=$1 $2"
    printf '{"ok": %s, "version": "%s", "message": "%s"}\n' "$1" "$VERSION" "$2" > "$RESULT"
}

running_from_app() {   # is any process still running from the app folder?
    if [ "$SYSTEM" = "macos" ]; then
        ps -axo comm= 2>/dev/null | grep -F -q "$APP/"
    else
        for exe in /proc/[0-9]*/exe; do
            target=$(readlink "$exe" 2>/dev/null) || continue
            case "$target" in "$APP"/*) return 0 ;; esac
        done
        return 1
    fi
}

wait_exit() {
    i=0
    while kill -0 "$PID" 2>/dev/null || running_from_app; do
        i=$((i + 1))
        [ "$i" -gt 180 ] && return 1
        sleep 0.5
    done
    return 0
}

restart() {
    if [ "$SYSTEM" = "macos" ]; then
        open "$APP" >/dev/null 2>&1 &
    else
        cd "$APP" && nohup "$APP/SimpleQuant" >/dev/null 2>&1 &
    fi
}

log "start: swap -> $VERSION, app $APP"
if ! wait_exit; then
    result false busy       # still running: change nothing and do not start a second copy
    exit 1
fi

ok=true
msg=""
rm -rf "$BACKUP"
if [ ! -d "$NEW" ]; then
    ok=false; msg="missing new folder"
elif ! mv "$APP" "$BACKUP"; then
    ok=false; msg="cannot move old folder"
elif ! mv "$NEW" "$APP"; then
    ok=false; msg="cannot move new folder"
    rm -rf "$APP"
    if ! mv "$BACKUP" "$APP"; then
        log "restore failed: old version is in $BACKUP"
    fi
fi
if [ "$ok" = true ]; then
    rm -rf "$BACKUP"
    result true ""
else
    result false "$msg"
fi
restart
[ "$ok" = true ]
