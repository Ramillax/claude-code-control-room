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

Entering "blocked": PermissionRequest fires the moment the dialog opens and names the tool
and its input. Notification(permission_prompt) is kept as a fallback for older Claude Code,
but it arrives ~6 s late (measured) and says nothing about which call is waiting.

Staying "blocked" with parallel tool calls: read-only calls run as a batch, so a sibling can
finish while another one waits for you. Its PreToolUse/PostToolUse must not clear the badge.
We remember each call waiting for permission as (tool name, input) and only a PostToolUse
for that same call leaves "blocked". (PermissionRequest carries no tool_use_id; two identical
calls in one batch match each other, which is harmless: the dialog is the same.)

Leaving on "no" or Esc: no hook fires at all, not even Stop. The server closes that gap by
reading the rejection the transcript does record, and as a fallback by checking the pane
(see effective_state in server/server.py), so this hook records both paths.

The hook must never break Claude: every error is swallowed and it always exits 0.
It does nothing when Claude is not running inside tmux (no session name to report).
"""
import fcntl, hashlib, json, os, subprocess, sys, time

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


def call_key(ev):
    """Which call this is: tool name + a hash of its input (PermissionRequest has no tool_use_id)."""
    raw = json.dumps(ev.get("tool_input"), sort_keys=True, ensure_ascii=False)
    return ev.get("tool_name", "") + ":" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def next_state(ev, cur):
    """(state, pending) after this event; state "" = keep the file as is, None = remove it.
    pending = the calls still waiting for your answer."""
    name = ev.get("hook_event_name", "")
    st, pending = cur.get("state", ""), list(cur.get("pending") or [])
    if name == "PermissionRequest":
        return "blocked", pending + [call_key(ev)]
    if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        if name != "PreToolUse" and call_key(ev) in pending:
            pending.remove(call_key(ev))          # this call was approved and ran
            return ("blocked" if pending else "working"), pending
        if st == "blocked" and pending:
            return "", pending                    # a sibling call: the dialog is still open
        return "working", []
    if name == "UserPromptSubmit":
        return "working", []
    if name in ("Stop", "SessionStart"):
        return "idle", []
    if name == "SessionEnd":
        return None, []                           # remove the file: the tile falls back to "shell"
    if name == "Notification":
        kind = ev.get("notification_type", "")
        msg = (ev.get("message") or "").lower()
        if kind == "permission_prompt" or "permission" in msg:
            return ("" if st == "blocked" else "blocked"), pending   # PermissionRequest got there first
        if kind == "idle_prompt" or "waiting for your input" in msg:
            return "idle", []
    return "", pending                            # other notifications: keep the current state


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    sess = tmux_session()
    if not sess or "/" in sess or sess.startswith("."):
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, sess + ".json")
    # parallel tool calls fire their hooks concurrently: read-modify-write under a lock
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(path) as fh:
                cur = json.load(fh)
        except Exception:
            cur = {}
        st, pending = next_state(ev, cur)
        if st == "" and pending == (cur.get("pending") or []):
            return
        if st is None:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        data = {"state": st or cur.get("state", ""), "pending": pending,
                "ts": int(time.time()) if st else cur.get("ts", 0),
                "event": ev.get("hook_event_name", ""),
                "claude_session_id": ev.get("session_id", ""),
                # every hook event carries it: the Chat view reads this session's conversation from
                # here, and the server looks in it for a rejected permission ("no" fires no hook)
                "transcript": ev.get("transcript_path", ""),
                # the exact pane Claude runs in, so the server can confirm "blocked" on screen
                "pane": os.environ.get("TMUX_PANE", "")}
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)        # atomic: the server never reads half a file


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
