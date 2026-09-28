#!/usr/bin/env python3
"""
cr-guard-hook — turns slog locks taken with --paths into something Claude Code ENFORCES.

Plain slog locks are advisory: a session that skips `slog status` can still overwrite the
file another session is working on. This PreToolUse hook closes that for file edits:

    slog take "api:auth" --paths 'src/auth/*'      # session A
    → session B tries Edit/Write on src/auth/login.py  →  DENIED, with who holds it and why

It also refuses direct writes to the board itself (SESSIONS.md): the only write path is
`slog`, which serializes writers with flock and replaces the file atomically. A session that
"cleans up" the board with a heredoc can silently drop another session's live lock.

What it covers, honestly:
  · Edit / Write / MultiEdit / NotebookEdit — exact: the tool says which file it writes.
  · Bash — BEST EFFORT. It looks for the usual write shapes (> file, >> file, tee, sed -i,
    mv/cp/rm/truncate/touch <target>) and checks those targets. A script that writes the file
    from the inside (python -c, a Makefile, git checkout) is not detected. It stops accidents,
    not a session determined to get around it.
  · Stale locks (older than SLOG_STALE_HOURS, default 4) are NOT enforced: a crashed session
    must never block everyone else. `slog status` shows them as ⚠stale.
  · Your own locks never block you. "You" = the tmux session name, same as slog (SLOG_TAG overrides).

Fails open: any error, missing slog, unreadable board → the tool call proceeds as if the hook
were not there. A guard that can wedge every session is worse than no guard.
"""
import fnmatch, json, os, re, shlex, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SLOG = os.path.join(os.path.dirname(HERE), "bin", "slog")
BOARD = os.environ.get("SLOG_BOARD") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "controlroom", "SESSIONS.md")

FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
# Bash: commands whose (non-option) arguments are write targets
WRITERS = {"tee", "truncate", "touch", "rm", "unlink", "shred"}
TARGET_LAST = {"mv", "cp", "install", "ln", "rsync"}


def me():
    if os.environ.get("SLOG_TAG"):
        return os.environ["SLOG_TAG"]
    if os.environ.get("TMUX"):
        cmd = ["tmux", "display-message", "-p"]
        if os.environ.get("TMUX_PANE"):
            cmd += ["-t", os.environ["TMUX_PANE"]]
        try:
            return subprocess.run(cmd + ["#S"], capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            pass
    return ""


def guarded_locks():
    """[(key, owner, globs)] for live (non-stale) locks that carry paths."""
    try:
        with open(BOARD, encoding="utf-8", errors="replace") as fh:
            if "{paths:" not in fh.read():
                return []                       # fast path: nobody uses --paths → no slog call
    except OSError:
        return []
    r = subprocess.run([SLOG, "locks"], capture_output=True, text=True, timeout=10,
                       env=dict(os.environ, SLOG_BOARD=BOARD))
    out = []
    for line in r.stdout.splitlines():
        p = line.split("\t")
        if len(p) >= 5 and p[3] != "stale" and p[4] not in ("", "-"):
            out.append((p[0], p[1], [g for g in p[4].split(",") if g]))
    return out


def norm(path, cwd):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def matches(path, globs):
    real = os.path.realpath(path)
    for g in globs:
        for cand in {path, real}:
            # fnmatch's * crosses "/", so 'src/auth/*' covers the whole subtree, and a
            # directory glob also covers the files under it
            if fnmatch.fnmatch(cand, g) or fnmatch.fnmatch(cand, g.rstrip("/") + "/*"):
                return True
    return False


def bash_targets(command):
    """Best-effort list of paths a shell command writes to."""
    targets = []
    for m in re.finditer(r"(?:^|[^<>&0-9])\d?>>?\|?\s*([^\s;&|<>()]+)", command):
        targets.append(m.group(1))                       # > file, >> file, 2> file
    for seg in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(seg, comments=True)
        except ValueError:
            words = seg.split()
        while words and re.fullmatch(r"\w+=.*", words[0]):
            words = words[1:]                            # FOO=bar cmd …
        while words and words[0] in ("sudo", "env", "nice", "nohup", "time", "command"):
            words = words[1:]
        if not words:
            continue
        cmd, args = os.path.basename(words[0]), [w for w in words[1:] if not w.startswith("-")]
        if cmd in WRITERS:
            targets += args
        elif cmd in TARGET_LAST and args:
            targets.append(args[-1])
            if cmd == "mv":
                targets += args[:-1]                     # moving a file away also changes it
        elif cmd in ("sed", "perl") and any(w == "-i" or w.startswith("-i") for w in words[1:]):
            targets += args[1:]                          # first non-option arg is the script
    return [t for t in targets if t and t not in ("/dev/null", "-") and not t.startswith("&")]


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


def main():
    ev = json.load(sys.stdin)
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input") or {}
    cwd = ev.get("cwd") or os.getcwd()

    if tool in FILE_TOOLS:
        paths = [inp.get(FILE_TOOLS[tool]) or ""]
    elif tool == "Bash":
        paths = bash_targets(inp.get("command") or "")
    else:
        return
    paths = [norm(p, cwd) for p in paths if p]
    if not paths:
        return

    board = os.path.normpath(BOARD)
    for p in paths:
        if p == board or os.path.realpath(p) == os.path.realpath(board):
            deny("The session board is only written through `slog` (it serializes writers and "
                 "replaces the file atomically; a direct write can drop another session's live "
                 "lock). Use `slog \"...\"`, `slog free <res>`, `slog done` or `slog trim`.")
            return

    locks = guarded_locks()
    if not locks:
        return
    who = me()
    for p in paths:
        for key, owner, globs in locks:
            if owner != who and matches(p, globs):
                deny(f"{p} is locked by session '{owner}' (slog lock '{key}'). Don't edit it now: "
                     f"run `slog status`, work on something else, or ask them to `slog free \"{key}\"`. "
                     "If that session is gone, the lock goes stale on its own after a few hours.")
                return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass                                             # fail open
    sys.exit(0)
