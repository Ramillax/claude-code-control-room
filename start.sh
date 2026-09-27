#!/usr/bin/env bash
# start.sh — run the control room in the foreground (Ctrl-C stops the UI, NOT your sessions).
#
#   ttyd    listens on a UNIX socket (no TCP port at all), one terminal per tile
#   server  serves the UI on CR_BIND:CR_PORT (default 127.0.0.1:7680) and proxies /tty/ to ttyd
#
# Your tmux sessions keep running after this script stops: tmux detaches from its client.
# For "always on", use the systemd units in examples/systemd/ instead.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="${CR_CONFIG:-$HERE/controlroom.env}"
if [ -f "$CONF" ]; then set -a; . "$CONF"; set +a
else echo "note: no $CONF — using defaults (copy controlroom.env.example to change them)"; fi

export CR_STATE_DIR="${CR_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/controlroom}"
export SLOG_BOARD="${SLOG_BOARD:-$CR_STATE_DIR/SESSIONS.md}"
export CR_TTYD_SOCKET="${CR_TTYD_SOCKET:-$CR_STATE_DIR/ttyd.sock}"
mkdir -p "$CR_STATE_DIR/state"

for bin in ttyd tmux python3; do
  command -v "$bin" >/dev/null || { echo "missing dependency: $bin" >&2; exit 1; }
done

# UNIX socket paths are limited to ~107 bytes by the kernel; fail loudly instead of ttyd's cryptic error
if [ "${#CR_TTYD_SOCKET}" -gt 100 ]; then
  echo "CR_TTYD_SOCKET path is too long for a UNIX socket (${#CR_TTYD_SOCKET} chars, max ~100): $CR_TTYD_SOCKET" >&2
  echo "set CR_STATE_DIR or CR_TTYD_SOCKET to a shorter path" >&2; exit 1
fi
rm -f "$CR_TTYD_SOCKET"
# -i <path>  bind to a UNIX socket: ttyd is reachable ONLY through server.py's /tty/ proxy
# -b /tty    base path (must match the proxy route)
# -W         writable terminals (ttyd >= 1.7 is read-only by default)
# -a         pass ?arg=<session> to cr-session as $1 (it is checked against CR_SESSIONS)
# -m 30      max clients: every tile + reconnects count; too low and new tiles are refused
# -P 30      WebSocket ping every 30 s so proxies don't drop idle terminals
ttyd -i "$CR_TTYD_SOCKET" -b /tty -W -a -m 30 -P 30 \
     -t fontSize=14 -t scrollback=10000 -t 'theme={"background":"#0b0e14","foreground":"#cdd6f4"}' \
     "$HERE/bin/cr-session" &
TTYD_PID=$!
python3 "$HERE/server/server.py" &
WEB_PID=$!
trap 'kill $TTYD_PID $WEB_PID 2>/dev/null; rm -f "$CR_TTYD_SOCKET"' EXIT INT TERM
echo "controlroom → http://${CR_BIND:-127.0.0.1}:${CR_PORT:-7680}   (Ctrl-C to stop; sessions keep running in tmux)"
wait -n $TTYD_PID $WEB_PID
