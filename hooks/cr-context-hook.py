#!/usr/bin/env python3
"""
cr-context-hook — the board comes to the session instead of waiting to be read.

The weak spot of advisory locks is that they only work if every session reads `slog status`
before touching shared things, and that depends on the agent following a line in CLAUDE.md.
This hook removes the "if": on SessionStart (new session, resume, /clear AND after compaction)
it injects into the session's context:

    · the live locks, and who holds them (or an explicit "none", only when slog answered)
    · the last lines of the board's feed, with closed TAKE/FREE pairs folded into one line
    · the project's notes (bin/cr-notes: done / next / don't redo)
    · the files OTHER sessions changed in the last hours (bin/cr-hist, if the project is recorded);
      this window's own changes stay out, also those of its conversation before a /clear

On SessionEnd it releases the locks this window still holds, so a session that exits cleanly
doesn't leave locks behind (a crash still can: those go ⚠stale after SLOG_STALE_HOURS and stop
being enforced).

Fails open and silent: any error → no context added, nothing released, exit 0.
"""
import json, os, re, subprocess, sys

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


def live_locks():
    """Active locks as TSV rows, or None if slog failed: "no locks" and "couldn't read them" differ."""
    try:
        r = subprocess.run([os.path.join(BIN, "slog"), "locks"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return [l.split("\t") for l in r.stdout.strip().splitlines() if l]


LINE = re.compile(r"^(\S+ (\d\d:\d\d)) · (\S+) · (.*?)\s*$")
TAKE = re.compile(r"^🔒 TAKE \[(.+?)\]")
FREE = re.compile(r"^✅ FREE \[(.+?)\](?: \(\S+ since (\d\d:\d\d)\))?\s*(.*)$")


def fold_locks(lines):
    """A TAKE whose FREE is also in view tells nothing on its own: the pair is dropped, and consecutive
    FREEs from the same session become ONE line with the intervals. Without this, four closed locks
    ate eight feed lines and pushed out the summary line, which is the one worth reading."""
    rows = [LINE.match(l) for l in lines]
    drop = set()
    for i, m in enumerate(rows):
        fr = m and FREE.match(m.group(4))
        if not fr:
            continue
        for j in range(i - 1, -1, -1):
            t = rows[j] and j not in drop and TAKE.match(rows[j].group(4))
            if t and t.group(1) == fr.group(1) and rows[j].group(3) == m.group(3):
                drop.add(j)
                break
    out, grp = [], None     # grp = (session, [items]) of the run of FREEs being folded
    for i, (l, m) in enumerate(zip(lines, rows)):
        if i in drop:
            continue
        fr = m and FREE.match(m.group(4))
        if not fr:
            out.append(l)
            grp = None
            continue
        item = f"{fr.group(1)} {fr.group(2) + '–' if fr.group(2) else '→'}{m.group(2)}" \
            + (f" ({fr.group(3)})" if fr.group(3) else "")
        if grp and grp[0] == m.group(3):
            grp[1].append(item)
        else:
            grp = (m.group(3), [item])
            out.append(None)
        out[-1] = f"{m.group(1)} · {m.group(3)} · 🔓 locks released: " + " · ".join(grp[1])
    return out


def session_start(ev):
    cwd = ev.get("cwd") or os.getcwd()
    me = my_tag()
    parts = []
    locks = live_locks()
    if locks:
        rows = []
        for p in locks:
            key, owner, since = p[0], p[1], p[2]
            stale = len(p) > 3 and p[3] == "stale"
            guards = p[4] if len(p) > 4 and p[4] not in ("", "-") else ""
            rows.append(f"- {key} — {owner}{' (you)' if owner == me else ''}, since {since}"
                        f"{' ⚠stale' if stale else ''}{'; guards ' + guards if guards else ''}")
        parts.append("Active locks:\n" + "\n".join(rows))
    elif locks is not None:   # confirmed empty; if slog failed, saying "none" would be a guess
        parts.append("Active locks: none.")
    # read extra lines: fold_locks() collapses closed TAKE/FREE pairs, so the feed shrinks
    tail = run([os.path.join(BIN, "slog"), "tail", str(FEED * 4)])
    feed = fold_locks([l.split(" · s:")[0] for l in tail.splitlines() if l.strip() and not l.startswith("#")])[-FEED:]
    if feed:
        parts.append("Recent board feed:\n" + "\n".join(feed))
    # Only OTHER sessions: /clear is a clean slate, so what this window's earlier conversation changed
    # stays out (it can't collide with anything, and it would pull the new conversation back into the
    # old line of work). cr-hist hides the window's commits when given only its tag; without a tmux
    # tag, the session id is the best it can do (hides this conversation only).
    env = dict(os.environ)
    if me:
        env["SLOG_TAG"] = me
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = ev.get("session_id") or env.get("CLAUDE_CODE_SESSION_ID", "")
    hist = run([os.path.join(BIN, "cr-hist"), "changed", str(CHANGED_HOURS)], cwd=cwd, env=env)
    # cr-hist ends with a "(N of … own hidden)" note (or is only that note when nobody else changed
    # anything): the files stay out, but one line says they exist and how to list them
    lines = [l for l in hist.splitlines() if not l.startswith("(")]
    own = re.search(r"(\d+) of this window's own hidden", hist)
    if lines:
        more = f"\n… {len(lines) - CHANGED_MAX} more: cr-hist changed {CHANGED_HOURS}" if len(lines) > CHANGED_MAX else ""
        parts.append(f"Files other sessions changed in the last {CHANGED_HOURS}h (cr-hist who <file> for detail):\n"
                     + "\n".join(lines[:CHANGED_MAX]) + more)
    if own and int(own.group(1)):
        parts.append(f"This window's own changes ({own.group(1)} in {CHANGED_HOURS}h, including any from before a /clear) "
                     f"are left out; `cr-hist changed {CHANGED_HOURS}` lists them if you need them.")
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
