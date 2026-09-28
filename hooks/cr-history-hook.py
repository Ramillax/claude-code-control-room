#!/usr/bin/env python3
"""
cr-history-hook — records every file a Claude session writes into the project's history.

PostToolUse hook. For each project registered with `cr-hist init` (a shadow git repo in
<project>/.controlroom/history.git), it commits what the session just changed:

  · Edit / Write / MultiEdit / NotebookEdit → exactly that file, attributed to this session.
  · Bash → the shell can write anything (heredoc, sed, a script), so the hook sweeps the
    registered projects and commits every changed file, marked `<session>/bash`. Attribution
    there is best effort: if another session left something uncommitted, this one picks it up.

One file per commit, never a global add: several sessions write in parallel, and a global
add would glue another session's half-finished work into this commit. Above MAX_SWEEP
files in one sweep they go into a single batch commit instead, so a script that generates
200 files doesn't flood the history.

Every commit carries a `Session: <CLAUDE_CODE_SESSION_ID>` trailer. That id is the
transcript's file name, so `cr-hist who <file>` leads straight to the conversation that
made the change.

Never blocks and never fails the tool call: every error is swallowed, exit 0.
"""
import fcntl, json, os, subprocess, sys

STATE = os.environ.get("CR_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "controlroom")
REG = os.path.join(STATE, "hist-repos")
FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
MAX_SWEEP = 25


def tag():
    if os.environ.get("SLOG_TAG"):
        return os.environ["SLOG_TAG"]
    if os.environ.get("TMUX"):
        cmd = ["tmux", "display-message", "-p"]
        if os.environ.get("TMUX_PANE"):
            cmd += ["-t", os.environ["TMUX_PANE"]]
        try:
            s = subprocess.run(cmd + ["#S"], capture_output=True, text=True, timeout=3).stdout.strip()
            if s:
                return s
        except Exception:
            pass
    return "pid%d" % os.getppid()


def repos():
    try:
        with open(REG) as fh:
            return [l.strip() for l in fh if l.strip() and os.path.isdir(os.path.join(l.strip(), ".controlroom", "history.git"))]
    except OSError:
        return []


def git(w, *args, **kw):
    return subprocess.run(["git", "--git-dir", os.path.join(w, ".controlroom", "history.git"),
                           "--work-tree", w, *args], capture_output=True, text=True, timeout=30, **kw)


def message(who, what, sid):
    msg = f"auto({who}): {what}"
    return msg + (f"\n\nSession: {sid}" if sid else "")


def commit_paths(w, paths, who, sid):
    """One commit per path (or one batch above MAX_SWEEP). Paths are relative to w."""
    if not paths:
        return
    if len(paths) > MAX_SWEEP:
        git(w, "add", "-A", "--", *paths)
        git(w, "commit", "-q", "-m", message(who, f"{len(paths)} files (batch)", sid))
        return
    for p in paths:
        git(w, "add", "-A", "--", p)                       # -A: also records a deletion
        if git(w, "diff", "--cached", "--quiet", "--", p).returncode != 0:
            git(w, "commit", "-q", "-m", message(who, p, sid), "--", p)


def changed(w):
    r = git(w, "status", "--porcelain", "-z", "--untracked-files=all")
    out, items = [], r.stdout.split("\0")
    i = 0
    while i < len(items):
        it = items[i]
        if len(it) > 3:
            out.append(it[3:])
            if it[0] in "RC":                              # rename: the next field is the old path
                i += 1
        i += 1
    return out


def main():
    ev = json.load(sys.stdin)
    tool = ev.get("tool_name", "")
    rs = repos()
    if not rs:
        return
    who, sid = tag(), os.environ.get("CLAUDE_CODE_SESSION_ID") or ev.get("session_id", "")
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "hist.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)                     # one committer at a time, all sessions
        if tool in FILE_TOOLS:
            path = (ev.get("tool_input") or {}).get(FILE_TOOLS[tool]) or ""
            if not path:
                return
            path = os.path.realpath(os.path.join(ev.get("cwd") or os.getcwd(), os.path.expanduser(path)))
            for w in sorted(rs, key=len, reverse=True):   # most specific project first
                wr = os.path.realpath(w)
                if path.startswith(wr + os.sep):
                    rel = os.path.relpath(path, wr)
                    if git(w, "check-ignore", "-q", "--", rel).returncode != 0:
                        commit_paths(w, [rel], who, sid)
                    return
        elif tool == "Bash":
            for w in rs:
                commit_paths(w, changed(w), who + "/bash", sid)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
