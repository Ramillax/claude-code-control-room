#!/usr/bin/env bash
# start-bg.sh — runs on every codespace start (postStartCommand): launch the panel detached.
# Log: /tmp/controlroom.log
WS="$(cd "$(dirname "$0")/.." && pwd)"
pgrep -f "$WS/server/server.py" >/dev/null && exit 0
setsid nohup bash "$WS/start.sh" > /tmp/controlroom.log 2>&1 < /dev/null &
sleep 2
tail -n 3 /tmp/controlroom.log
