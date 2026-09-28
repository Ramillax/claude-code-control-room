#!/usr/bin/env python3
"""
cr-state-hook — tells the control room what each Claude Code session is doing.

Registered as a Claude Code hook (see examples/claude-settings-hooks.json). Claude Code
runs it on lifecycle events and passes a JSON payload on stdin. We map the event to a
simple state and write it to  $CR_STATE_DIR/state/<tmux-session>.json , which the web
server reads to paint a dot on each tile and raise a badge when a session needs you.

    working  — you sent a prompt / a tool is running
    blocked  — Claude is waiting for YOUR permission (the one that matters on a phone)
    idle     — finished its turn, waiting for a new prompt

Why hooks and not screen-scraping: reading the terminal with regexes breaks every time
the Claude Code UI changes. Hook events are a documented interface.

The hook must never break Claude: every error is swallowed and it always exits 0.
It does nothing when Claude is not running inside tmux (no session name to report).
"""
import json, os, subprocess, sys, time

STATE_DIR = os.path.join(
    os.environ.get("CR_STATE_DIR")
    or os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "controlroom"),
    "state")


def tmux_session():
    if not os.environ.get("TMUX"):
        return ""
    cmd = ["tmux", "display-message", "-p"]
    pane = os.environ.get("TMUX_PANE")
    if pane:
        cmd += ["-t", pane]          # the pane Claude runs in, not whichever one is focused
    try:
        out = subprocess.run(cmd + ["#S"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except Exception:
        return ""


def state_for(ev):
    name = ev.get("hook_event_name", "")
    if name in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "working"
    if name in ("Stop", "SessionStart"):
        return "idle"
    if name == "SessionEnd":
        return None                  # remove the file: the tile falls back to "shell"
    if name == "Notification":
        kind = ev.get("notification_type", "")
        msg = (ev.get("message") or "").lower()
        if kind == "permission_prompt" or "permission" in msg:
            return "blocked"
        if kind == "idle_prompt" or "waiting for your input" in msg:
            return "idle"
        return ""                    # other notifications: keep the current state
    return ""


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    sess = tmux_session()
    if not sess or "/" in sess or sess.startswith("."):
        return
    st = state_for(ev)
    if st == "":
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, sess + ".json")
    if st is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    data = {"state": st, "ts": int(time.time()), "event": ev.get("hook_event_name", ""),
            "claude_session_id": ev.get("session_id", "")}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)            # atomic: the server never reads half a file


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
