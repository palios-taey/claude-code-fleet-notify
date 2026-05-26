#!/usr/bin/env bash
# Start the notification delivery daemon — handles all local tmux sessions.
#
# The daemon watches Redis inboxes (${NOTIFY_KEY_PREFIX:-taey}:{session}:inbox) and injects
# messages via tmux when sessions are idle.
#
# Uses setsid to fully detach from calling terminal — survives shell exits.
#
# Usage:
#   bash scripts/start_notify_daemons.sh          # start the daemon
#   bash scripts/start_notify_daemons.sh stop      # kill the daemon
#   bash scripts/start_notify_daemons.sh status    # show daemon status
#   bash scripts/start_notify_daemons.sh restart   # stop + start

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DAEMON="$REPO_DIR/notifications/daemon.py"
REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
NOTIFY_KEY_PREFIX="${NOTIFY_KEY_PREFIX:-taey}"
PIDDIR="/tmp/notify-daemons"
PIDFILE="$PIDDIR/daemon.pid"
LOGFILE="/tmp/notify-daemon.log"

mkdir -p "$PIDDIR"

start_daemon() {
    # Kill existing if running according to pidfile
    if [ -f "$PIDFILE" ]; then
        local old_pid
        old_pid=$(cat "$PIDFILE" 2>/dev/null || echo "")
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            echo "[notify] Daemon already running (pid=$old_pid). Use restart if needed."
            return
        fi
        rm -f "$PIDFILE"
    fi

    # Check for orphaned daemons before starting
    if pgrep -f "notifications/daemon.py" > /dev/null; then
        echo "[notify] WARNING: Orphaned daemon processes found. Cleaning up..."
        pkill -f "notifications/daemon.py" || true
        sleep 1
    fi

    # Start with setsid to fully detach
    echo "[notify] Starting notification daemon..."
    setsid python3 "$DAEMON" \
        --redis-host "$REDIS_HOST" \
        --redis-port "$REDIS_PORT" \
        >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"
    echo "[notify] Started daemon (pid=$pid). Logs: $LOGFILE"
}

stop_daemon() {
    echo "[notify] Stopping notification daemon..."
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "[notify] Killed daemon (pid=$pid)"
        else
            echo "[notify] Daemon not running (stale pidfile)"
        fi
        rm -f "$PIDFILE"
    else
        echo "[notify] No pidfile found. Attempting pkill..."
    fi
    # Also kill any orphaned daemons
    pkill -f "notifications/daemon.py" 2>/dev/null || true
}

show_status() {
    echo "=== Notification Daemon Status ==="
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "  [UP]   Daemon is running (pid=$pid)"
            echo "  Logs:  $LOGFILE"
            echo "  Redis: $REDIS_HOST:$REDIS_PORT prefix=$NOTIFY_KEY_PREFIX"
            echo "  Sessions handled: $(tmux list-sessions -F '#S' 2>/dev/null | wc -l || echo 0) active tmux sessions"
        else
            echo "  [DOWN] Daemon pidfile exists but process is dead (pid=$pid)"
        fi
    else
        # Check if it's running without pidfile
        local actual_pid
        actual_pid=$(pgrep -f "notifications/daemon.py" | head -n 1 || echo "")
        if [ -n "$actual_pid" ]; then
            echo "  [UP]   Daemon is running (pid=$actual_pid) but NO pidfile found!"
        else
            echo "  [DOWN] Daemon is not running."
        fi
    fi
}

case "${1:-start}" in
    start)
        if [ ! -f "$DAEMON" ]; then
            echo "ERROR: $DAEMON not found" >&2
            exit 1
        fi
        start_daemon
        ;;
    stop)
        stop_daemon
        ;;
    restart)
        stop_daemon
        sleep 1
        start_daemon
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
