#!/usr/bin/env python3
"""
cr-context-hook — the board comes to the session instead of waiting to be read.

The weak spot of advisory locks is that they only work if every session reads `slog status`
before touching shared things, and that depends on the agent following a line in CLAUDE.md.
This hook removes the "if": on SessionStart (new session, resume, /clear AND after compaction)
it injects into the session's context:

    · the live locks, and who holds them
    · the last lines of the board's feed
    · the project's notes (bin/cr-notes: done / next / don't redo)
    · the files other sessions changed in the last hours (bin/cr-hist, if the project is recorded),
      including what an earlier conversation of this same window changed before a /clear

On SessionEnd it releases the locks this window still holds, so a session that exits cleanly
doesn't leave locks behind (a crash still can: those go ⚠stale after SLOG_STALE_HOURS and stop
being enforced).

Fails open and silent: any error → no context added, nothing released, exit 0.
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin")
MAX_NOTES = 40          # lines; the notes are meant to be short, this caps a runaway file
FEED = 8
CHANGED_HOURS = 12   # cr-hist: what other sessions changed recently (only if the project is recorded)
CHANGED_MAX = 10


def run(args, cwd=None, env=None):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10, cwd=cwd, env=env)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def my_tag():
    if os.environ.get("SLOG_TAG"):
        return os.environ["SLOG_TAG"]
    if os.environ.get("TMUX"):
        cmd = ["tmux", "display-message", "-p"]
        if os.environ.get("TMUX_PANE"):
            cmd += ["-t", os.environ["TMUX_PANE"]]
        return run(cmd + ["#S"])
    return ""


def session_start(ev):
    cwd = ev.get("cwd") or os.getcwd()
    me = my_tag()
    parts = []
    locks = [l.split("\t") for l in run([os.path.join(BIN, "slog"), "locks"]).splitlines() if l]
    if locks:
        rows = []
        for p in locks:
            key, owner, since = p[0], p[1], p[2]
            stale = len(p) > 3 and p[3] == "stale"
            guards = p[4] if len(p) > 4 and p[4] not in ("", "-") else ""
            rows.append(f"- {key} — {owner}{' (you)' if owner == me else ''}, since {since}"
                        f"{' ⚠stale' if stale else ''}{'; guards ' + guards if guards else ''}")
        parts.append("Active locks:\n" + "\n".join(rows))
    tail = run([os.path.join(BIN, "slog"), "tail", str(FEED + 4)])
    feed = [l.split(" · s:")[0] for l in tail.splitlines() if l.strip() and not l.startswith("#")][-FEED:]
    if feed:
        parts.append("Recent board feed:\n" + "\n".join(feed))
    # cr-hist hides only THIS conversation's commits (Session: trailer), so after /clear the session
    # still sees what its own window's earlier conversation changed
    hist = run([os.path.join(BIN, "cr-hist"), "changed", str(CHANGED_HOURS)], cwd=cwd,
               env={**os.environ, "CLAUDE_CODE_SESSION_ID": ev.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")})
    if hist and not hist.startswith("("):
        lines = hist.splitlines()
        more = f"\n… {len(lines) - CHANGED_MAX} more: cr-hist changed {CHANGED_HOURS}" if len(lines) > CHANGED_MAX else ""
        parts.append(f"Files other sessions changed in the last {CHANGED_HOURS}h (cr-hist who <file> for detail):\n"
                     + "\n".join(lines[:CHANGED_MAX]) + more)
    notes = run([os.path.join(BIN, "cr-notes"), "show"], cwd=cwd)
    if notes and not notes.startswith("("):
        lines = notes.splitlines()
        extra = f"\n… {len(lines) - MAX_NOTES} more: cr-notes show" if len(lines) > MAX_NOTES else ""
        parts.append("Project notes (done / next / don't redo):\n" + "\n".join(lines[:MAX_NOTES]) + extra)
    if not parts:
        return
    head = (f"[control room] You are tmux session '{me}'. " if me else "[control room] ") + \
        "Other Claude sessions may be working in parallel. Before touching something another " \
        "session holds, coordinate: `slog status`, `slog take <res>`, `slog free <res>`."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": head + "\n\n" + "\n\n".join(parts)}}))


def session_end(ev):
    me = my_tag()
    if not me:
        return
    for line in run([os.path.join(BIN, "slog"), "locks"]).splitlines():
        p = line.split("\t")
        if len(p) >= 2 and p[1] == me:
            run([os.path.join(BIN, "slog"), "free", p[0], f"(auto: session ended — {ev.get('reason', '')})"])


def main():
    ev = json.load(sys.stdin)
    name = ev.get("hook_event_name", "")
    if name == "SessionStart":
        session_start(ev)
    elif name == "SessionEnd":
        session_end(ev)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
