#!/bin/sh
# Reach the dashboard from another network, without forwarding a single port.
#
#   scripts/remote-access.sh status      what is set up, and the URL to open
#   sudo scripts/remote-access.sh setup  install, log in, publish the dashboard
#   sudo scripts/remote-access.sh printer  also route to the printer's own UI
#   sudo scripts/remote-access.sh off    stop accepting tailnet traffic
#
# Tailscale rather than a public tunnel: there is no public hostname, so the
# printer's unauthenticated web UI is never one hostname typo away from the
# internet. `.ts.net` names only resolve inside your own tailnet.
#
# Everything here is idempotent; running `setup` twice changes nothing.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${EC_ENV_FILE:-}" ]; then
    ENV_FILE=$EC_ENV_FILE
elif [ -f "$HOME/.elegoo-bridge.env" ]; then
    ENV_FILE=$HOME/.elegoo-bridge.env
else
    ENV_FILE=$ROOT/.env
fi

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

PORT=${EC_PORT:-8787}
PRINTER=${EC_PRINTER_HOST:-}

have() {
    command -v "$1" >/dev/null 2>&1
}

need_root() {
    if [ "$(id -u)" != 0 ]; then
        echo "this needs root:  sudo sh $0${1:+ $1}" >&2
        exit 1
    fi
}

# --- status ----------------------------------------------------------------

status() {
    if ! have python3; then
        echo "python3 missing - this subcommand needs it" >&2
        return 1
    fi
    PORT="$PORT" PRINTER="$PRINTER" python3 - <<'PY'
import json
import os
import shutil
import subprocess
import sys

port = os.environ.get("PORT", "8787")
printer = os.environ.get("PRINTER", "")


def run(*args):
    try:
        done = subprocess.run(["tailscale", *args], capture_output=True, text=True, timeout=20)
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


if not shutil.which("tailscale"):
    print("tailscale     not installed")
    print("next          sudo sh scripts/remote-access.sh setup")
    sys.exit(1)

version = (run("version") or "").splitlines()
print(f"tailscale     {(version[0] if version else 'installed').strip()}")

raw = run("status", "--json")
if raw is None:
    print("              daemon is not answering")
    print("next          sudo systemctl status tailscaled")
    sys.exit(1)

data = json.loads(raw)
me = data.get("Self") or {}
state = data.get("BackendState") or "unknown"
name = (me.get("DNSName") or "").rstrip(".")
ips = me.get("TailscaleIPs") or []
ip = ips[0] if ips else ""

print(f"              {state}, {name or 'no name'}, {ip or 'no address'}")
if state != "Running" or not ip:
    print("next          sudo tailscale up   (then approve the machine in the browser)")
    sys.exit(1)

print(f"dashboard     http://{ip}:{port}")
# MagicDNS name, so the same address survives a changed tailnet IP.
if name:
    print(f"magicdns      http://{name}:{port}")

served = run("serve", "status")
if served is None:
    print("serve         unknown (needs root: tailscale serve status)")
elif "https://" in served:
    for line in served.splitlines():
        if "https://" in line:
            print(f"serve         {line.strip()}")
            break
else:
    print("serve         off - both addresses above already work over WireGuard")
    if data.get("CertDomains"):
        print("next          sudo tailscale serve --bg " + port + "   (turns it into an https:// name)")
    else:
        print("next          admin console -> DNS -> HTTPS Certificates -> Enable,")
        print("              then sudo tailscale serve --bg " + port)

if printer:
    print(f"printer       advertised route for {printer}/32 is set by 'setup printer'")
else:
    print("printer       no EC_PRINTER_HOST in the env file, so no route is offered")
print("")
print("From the phone: WiFi off, Tailscale on, open the dashboard line above.")
PY
}

# --- setup -----------------------------------------------------------------

enable_forwarding() {
    if [ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)" = "1" ]; then
        return 0
    fi
    printf 'net.ipv4.ip_forward = 1\n' >/etc/sysctl.d/99-tailscale.conf
    sysctl -q -p /etc/sysctl.d/99-tailscale.conf
    echo "ip forward    enabled (the Pi can route to the printer)"
}

serve_dashboard() {
    if tailscale serve --bg "$PORT" >/dev/null 2>&1; then
        url=$(tailscale serve status 2>/dev/null | awk '/https:\/\// { print $1; exit }')
        echo "serve         ${url:-the dashboard is published on this machine's .ts.net name}"
    else
        echo "serve         no https:// name yet; the plain tailnet address still works."
        echo "              fix: admin console -> DNS -> HTTPS Certificates -> Enable,"
        echo "              then: sudo tailscale serve --bg $PORT"
    fi
}

route_printer() {
    if [ -z "$PRINTER" ]; then
        echo "printer       skipped: no EC_PRINTER_HOST in $ENV_FILE" >&2
        return 0
    fi
    if tailscale set --advertise-routes="$PRINTER/32" 2>/dev/null; then
        echo "printer       advertising $PRINTER/32"
        echo "              approve it: admin console -> Machines -> this Pi -> Edit route settings"
    else
        echo "printer       could not advertise $PRINTER/32" >&2
        return 1
    fi
}

setup() {
    want_printer=${1:-}
    need_root "setup${want_printer:+ $want_printer}"

    if ! have tailscale; then
        echo "installing tailscale (this pulls a few MB)..."
        curl -fsSL https://tailscale.com/install.sh | sh
    fi
    systemctl enable --now tailscaled >/dev/null 2>&1 || true

    if ! tailscale status >/dev/null 2>&1; then
        echo
        echo "Open the URL that appears below on any device, sign in, approve this Pi."
        echo
        tailscale up
    else
        echo "tailscale     already signed in"
    fi

    enable_forwarding
    serve_dashboard
    if [ "$want_printer" = "printer" ]; then
        route_printer
    fi

    echo
    status
}

off() {
    tailscale serve reset >/dev/null 2>&1 || true
    tailscale down >/dev/null 2>&1 || echo "tailscale was already down"
    echo "tailnet traffic stopped; 'sudo sh $0 setup' brings it back"
}

case "${1:-status}" in
    status) status ;;
    setup) setup "${2:-}" ;;
    printer) need_root printer; route_printer ;;
    off) need_root off; off ;;
    *)
        echo "usage: $0 [status|setup [printer]|printer|off]" >&2
        exit 2
        ;;
esac
