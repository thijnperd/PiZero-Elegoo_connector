#!/bin/sh
# Start, stop or restart the bridge with settings from a private env file.
#
# The password lives in ~/.elegoo-bridge.env (mode 600) instead of on the
# command line, where `ps` would show it to every user on the Pi. This is also
# what cron calls at boot, until a proper systemd unit can be installed:
#
#   @reboot /home/thijn/Elegoo_connector/scripts/start-bridge.sh >>/home/thijn/elegoo-bridge.log 2>&1
#
# Usage: scripts/start-bridge.sh [start|stop|restart|ensure]
#
# 'ensure' starts the bridge only when it is not already running, and stays quiet
# otherwise, so cron can call it every couple of minutes as a watchdog.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE=${EC_ENV_FILE:-$HOME/.elegoo-bridge.env}
#: Match the interpreter's own command line, never this script's.
PATTERN='-m elegoo_bridge$'

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

is_running() {
    pgrep -f -- "$PATTERN" >/dev/null 2>&1
}

# SIGTERM makes uvicorn drain its connections; it is not gone instantly. Waiting
# for the port to actually close is what keeps a restart from losing the race and
# leaving nothing running.
wait_stopped() {
    tries=0
    while is_running && [ "$tries" -lt 20 ]; do
        tries=$((tries + 1))
        sleep 0.5
    done
    is_running && echo "warning: elegoo_bridge still running after 10s" >&2
    return 0
}

case "${1:-start}" in
    start)
        if is_running; then
            echo "elegoo_bridge is already running"
            exit 0
        fi
        cd "$ROOT/bridge"
        exec "$ROOT/.venv/bin/python" -m elegoo_bridge
        ;;
    stop)
        if is_running; then
            pkill -f -- "$PATTERN"
            wait_stopped
            echo "elegoo_bridge stopped"
        else
            echo "elegoo_bridge is not running"
        fi
        ;;
    restart)
        if is_running; then
            pkill -f -- "$PATTERN"
            wait_stopped
        fi
        cd "$ROOT/bridge"
        exec "$ROOT/.venv/bin/python" -m elegoo_bridge
        ;;
    ensure)
        is_running && exit 0
        cd "$ROOT/bridge"
        exec "$ROOT/.venv/bin/python" -m elegoo_bridge
        ;;
    *)
        echo "usage: $0 [start|stop|restart|ensure]" >&2
        exit 2
        ;;
esac
