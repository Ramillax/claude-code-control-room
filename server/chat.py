"""
chat.py — the Chat view of each tile, and the plan-usage meters (used by server.py).

  GET  /api/chat?s=<session>&sid=<id>&off=<byte>  → NEW events of that session's transcript
  GET  /api/usage                                → plan usage (session / week) + context % per tile
  POST /chat/send   (form: s, text | key, f=files) → types into the tmux session

Which transcript belongs to which tile: the state hook records `transcript_path` on every event
(state/<session>.json) and the silent statusLine does too (usage/<session>.json); the newest wins,
so it follows /clear, /resume and compaction on its own. No hooks → no Chat view for that tile
(the terminal still works).

Reading is incremental by byte offset; the first load reads only the tail of the file. The
terminal keeps running UNDER the chat overlay — the chat never replaces it.
"""
import glob, json, os, re, subprocess, time

TAIL_BYTES = 600_000
TAIL_MAX = 16_000_000          # screenshots Claude reads are >1 MB per line: widen the tail until we have enough
RES_MAX = 2500
KEYS = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "Escape", "Enter", "Up", "Down", "BTab", "C-c", "Left", "Right"}   # BTab = Shift+Tab
# Same criterion as server.PERMISSION_BOX: a permission box / a question with options at the bottom
RE_WATCH = re.compile(r"❯ 1\.|No, and tell Claude what to do|Do you want to proceed\?|Do you trust the files")
OPT = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.+?)\s*$")
SPIN = re.compile(r"^\s*[·✢✳✶✻✽*]\s+([A-Z][^…]{1,40}…)\s*(?:\((.*)\))?\s*$")

TMUX = ["tmux"]
STATE_DIR = ""
DL_ROOTS = []                  # = server.ALLOWED_ROOTS: a file Claude mentions gets a Download card only inside them
FILE_RE = re.compile(r"((?:~|(?<![\w.~]))/(?:[\w.@+-]+/)+[\w.@+-]+\.[A-Za-z0-9]{1,8})\b")   # absolute or ~/
PREVIEW_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}   # these get a thumbnail / viewer instead


def init(tmux, state_dir, dl_roots=()):
    global TMUX, STATE_DIR, DL_ROOTS
    TMUX, STATE_DIR, DL_ROOTS = tmux, state_dir, list(dl_roots)


def _human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def files_in(text):
    """Files Claude hands you in a reply: a path that EXISTS inside the download roots → a Download card."""
    out, seen = [], set()
    for m in FILE_RE.finditer(text):
        p = m.group(1)
        if p in seen or os.path.splitext(p)[1].lower() in PREVIEW_EXT:
            continue
        seen.add(p)
        rp = os.path.realpath(os.path.expanduser(p))
        if os.path.isfile(rp) and any(rp.startswith(r + os.sep) for r in DL_ROOTS):
            out.append({"p": rp, "n": os.path.basename(rp), "size": _human(os.path.getsize(rp))})
        if len(out) >= 6:
            break
    return out


def _run(args, inp=None, timeout=5):
    try:
        return subprocess.run(args, input=inp, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh), os.path.getmtime(path)
    except (OSError, ValueError):
        return {}, 0


def transcript_for(sess):
    """→ (path | None, fresh). fresh=True: the records name a transcript that doesn't exist YET — a new
    conversation (Claude Code creates the file with the first message). Never guess another file then."""
    cands = []
    d, mt = _read_json(os.path.join(STATE_DIR, "state", sess + ".json"))
    if d.get("transcript"):
        cands.append((mt, d["transcript"]))
    d, mt = _read_json(os.path.join(STATE_DIR, "usage", sess + ".json"))
    if d.get("transcript_path"):
        cands.append((mt, d["transcript_path"]))
    if not cands:
        return None, False
    _, p = max(cands)
    return (p, False) if os.path.isfile(p) else (None, True)


def _capture(sess, history=0):
    args = TMUX + ["capture-pane", "-p", "-t", f"={sess}:"] + (["-S", str(-history)] if history else [])
    r = _run(args)
    return r.stdout if r and r.returncode == 0 else None


def pane_mode(sess, cap=None):
    """Claude Code's permission mode, read from the footer of its UI."""
    cap = cap if cap is not None else _capture(sess)
    if not cap:
        return ""
    if "auto mode on" in cap:
        return "auto"
    if "accept edits on" in cap:
        return "accept-edits"
    if "plan mode on" in cap:
        return "plan"
    if re.search(r"manual mode on|\? for shortcuts|esc to interrupt|Claude Code v", cap):
        return "normal"
    return ""


def _strip(t):
    return re.sub(r"<system-reminder>.*?</system-reminder>", "", t, flags=re.S).strip()


def _events(lines):
    out = []
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        t, ts = e.get("type"), e.get("timestamp", "")
        if t == "attachment" and (e.get("attachment") or {}).get("type") == "queued_command":
            p = str(e["attachment"].get("prompt", "")).strip()
            if p:
                out.append({"k": "user", "text": p, "ts": ts})
            continue
        if t not in ("user", "assistant") or e.get("isMeta"):
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, str):
            c = _strip(c)
            m = re.search(r"<command-name>(/[^<]+)</command-name>", c)
            if m:                                   # a /skill or /clear you typed: a visible chip in the chat
                a = re.search(r"<command-args>(.*?)</command-args>", c, re.S)
                out.append({"k": "cmd", "name": m.group(1).strip(), "args": (a.group(1).strip() if a else "")[:200], "ts": ts})
                continue
            if c and not c.startswith(("<command-", "<local-command")):
                out.append({"k": "user", "text": c, "ts": ts})
            continue
        for part in c or []:
            pt = part.get("type")
            if pt == "text":
                txt = _strip(part.get("text", ""))
                if txt:
                    ev = {"k": "asst" if t == "assistant" else "user", "text": txt, "ts": ts}
                    if t == "assistant":
                        fs = files_in(txt)
                        if fs:
                            ev["files"] = fs
                    out.append(ev)
            elif pt == "tool_use":
                inp = part.get("input") or {}
                slim = {k: (v[:4000] if isinstance(v, str) else v) for k, v in inp.items()}
                out.append({"k": "tool", "id": part.get("id"), "name": part.get("name"), "input": slim, "ts": ts})
            elif pt == "tool_result":
                rc = part.get("content")
                if isinstance(rc, list):
                    rc = "\n".join(x.get("text", "[image]" if x.get("type") == "image" else "") for x in rc)
                rc = str(rc or "")
                out.append({"k": "res", "id": part.get("tool_use_id"), "err": bool(part.get("is_error")),
                            "text": rc if len(rc) <= RES_MAX else rc[:RES_MAX] + "\n… (truncated)"})
    return out


def screen(sess, cap=None):
    """When the session waits for an answer (permission / a question): the question, its options
    (joined when the UI wrapped them) and the box's detail (WHAT is being asked: command, file…)."""
    cap = cap if cap is not None else _capture(sess, 60)
    if not cap:
        return None
    lines = [l.replace("│", " ").rstrip() for l in cap.splitlines() if l.strip()]
    if not RE_WATCH.search("\n".join(lines[-14:])):
        return None
    first = next((i for i in range(len(lines) - 1, -1, -1)
                  if OPT.match(lines[i]) and OPT.match(lines[i]).group(1) == "1"), None)
    if first is None:
        return None
    opts = []
    for l in lines[first:]:
        m = OPT.match(l)
        if m:
            opts.append({"n": m.group(1), "label": m.group(2)})
        elif opts and l.startswith("   ") and not re.match(r"^\s*(Esc to|Enter to|Tab to)", l):
            opts[-1]["label"] += " " + l.strip()          # continuation of the previous option
        else:
            break
    qi = first - 1
    q = lines[qi].strip() if qi >= 0 and lines[qi].strip().endswith("?") else ""
    # detail = what sits between the last ─── rule and the question, minus the UI's "Tip:" paragraph
    top = qi if q else first
    rule = next((i for i in range(top - 1, -1, -1) if re.match(r"^\s*[─━-]{8,}\s*$", lines[i])), max(0, top - 14))
    detail, tip = [], False
    for l in lines[rule + 1:top]:
        if l.strip().startswith("Tip:"):
            tip = True
            continue
        if tip and l.startswith(" ") and not l.startswith("   "):
            continue
        tip = False
        detail.append(l)
    for o in opts:
        o["label"] = o["label"][:120]
    return {"question": q[:200], "opts": opts[:9], "detail": "\n".join(detail)[:1500]}


def spinner(sess, cap=None):
    """Claude's animated working line: '· Swirling… (2m 33s · ↓ 10.4k tokens · thought for 8s)'."""
    cap = cap if cap is not None else _capture(sess)
    for l in reversed([x for x in (cap or "").splitlines() if x.strip()][-14:]):
        m = SPIN.match(l)
        if m:
            return {"verb": m.group(1), "meta": (m.group(2) or "")[:120]}
    return None


def get(sess, sid, off, allowed):
    if sess not in allowed:
        return {"error": "session not allowed"}
    r = _run(TMUX + ["display-message", "-p", "-t", f"={sess}:", "#{pane_current_command}"])
    cmd = r.stdout.strip() if r and r.returncode == 0 else ""
    if not cmd:
        return {"s": sess, "off": True, "sid": "", "events": []}
    if cmd not in ("claude", "node"):
        # no Claude in the foreground: the shell (it exited with /exit, or never started) or another program (ssh, vim…)
        return {"s": sess, "away": True, "shell": bool(re.match(r"^-?(bash|zsh|sh|fish|dash)$", cmd)),
                "cmd": cmd, "sid": "", "off": 0, "events": [], "screen": None}
    cap = _capture(sess, 60)
    res = {"s": sess, "screen": screen(sess, cap), "spin": spinner(sess, cap)}
    path, fresh = transcript_for(sess)
    if not path:
        return {**res, "sid": "", "off": 0, "events": [], "reset": True, "fresh": fresh, "nohooks": not fresh}
    cur = os.path.basename(path)[:-6]
    size = os.path.getsize(path)
    reset = cur != sid or off > size or off <= 0
    tail = TAIL_BYTES
    while True:
        start = max(0, size - tail) if reset else off
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
        skip = 0
        if reset and start > 0:            # the tail starts mid-line: drop that partial line
            i = data.find(b"\n")
            skip = i + 1 if i >= 0 else len(data)
            data = data[skip:]
        cut = data.rfind(b"\n") + 1        # complete lines only: the last one may be half-written
        events = _events(data[:cut].decode("utf-8", "replace").splitlines())
        if not reset or start == 0 or len(events) >= 60 or tail >= TAIL_MAX:
            break
        tail *= 3
    return {**res, "sid": cur, "off": start + skip + cut, "events": events,
            "reset": reset, "partial": reset and start > 0}


def send(sess, allowed, text=None, key=None):
    if sess not in allowed:
        return False
    tgt = f"={sess}:"
    if key:
        if key not in KEYS:
            return False
        r = _run(TMUX + ["send-keys", "-t", tgt, key])
        return bool(r and r.returncode == 0)
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return False
    # bracketed paste: line breaks don't submit the message halfway through
    buf = "cr-chat-" + sess
    r = _run(TMUX + ["load-buffer", "-b", buf, "-"], inp=text)
    if not r or r.returncode != 0:
        return False
    r = _run(TMUX + ["paste-buffer", "-p", "-d", "-b", buf, "-t", tgt])
    if not r or r.returncode != 0:
        return False
    time.sleep(0.25)
    r = _run(TMUX + ["send-keys", "-t", tgt, "Enter"])
    return bool(r and r.returncode == 0)


def usage():
    """Plan limits (5-hour session and weekly) + context % per tile, from what cr-statusline saved.
    The limits belong to the ACCOUNT, not the tile: for each one, take the newest file that has it."""
    out = {"five_hour": None, "seven_day": None, "ctx": {}, "model": {}}
    best = {}
    for p in glob.glob(os.path.join(STATE_DIR, "usage", "*.json")):
        sess = os.path.basename(p)[:-5]
        d, mt = _read_json(p)
        cw = d.get("context_window") or {}
        if cw.get("used_percentage") is not None:
            out["ctx"][sess] = cw["used_percentage"]
        out["model"][sess] = (d.get("model") or {}).get("display_name", "")
        for k in ("five_hour", "seven_day"):
            v = (d.get("rate_limits") or {}).get(k)
            if v and (k not in best or mt > best[k]):
                best[k] = mt
                out[k] = {"pct": v.get("used_percentage"), "resets_at": v.get("resets_at"), "age": int(time.time() - mt)}
    return out
