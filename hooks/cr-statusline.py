#!/usr/bin/env python3
"""
cr-statusline — a SILENT Claude Code statusLine that feeds the control room.

Claude Code runs the statusLine command on every refresh and passes a JSON payload on stdin:
model, context window usage, the transcript path and — on Pro/Max plans — the plan's rate
limits (`rate_limits.five_hour` and `rate_limits.seven_day`, each with used_percentage and
resets_at). We save it per tmux session to $CR_STATE_DIR/usage/<session>.json; the web UI shows
the plan usage in the top bar and the context % on each tile.

It prints nothing, so it adds no line to Claude's UI. If you already have a statusLine you like,
keep it and pipe the same stdin to this script too:  tee >(python3 .../cr-statusline.py) | your-script
Every error is swallowed: a status line must never break Claude.
"""
import json, os, subprocess, sys

STATE_DIR = (os.environ.get("CR_STATE_DIR")
             or os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "controlroom"))
OUT = os.path.join(STATE_DIR, "usage")


def tmux_session():
    if not os.environ.get("TMUX"):
        return ""
    cmd = ["tmux", "display-message", "-p"] + (["-t", os.environ["TMUX_PANE"]] if os.environ.get("TMUX_PANE") else [])
    try:
        return subprocess.run(cmd + ["#S"], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


def main():
    ev = json.load(sys.stdin)
    sess = tmux_session()
    if not sess or "/" in sess or sess.startswith("."):
        return
    keep = {k: ev.get(k) for k in ("session_id", "transcript_path", "model", "context_window", "rate_limits", "version")}
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, "." + sess + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(keep, fh)
    os.replace(tmp, os.path.join(OUT, sess + ".json"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
