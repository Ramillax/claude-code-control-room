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
            if pt == "thinking":                    # the narration the terminal shows as ● text (the signature alone when empty)
                txt = (part.get("thinking") or "").strip()
                if txt:
                    out.append({"k": "think", "text": txt[:4000], "ts": ts})
                continue
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
    q = lines[qi].strip() if qi >= 0 and lines[qi].strip().endswith(("?", ":")) else ""
    # detail = what sits between the last ─── rule and the question, minus the UI's "Tip:" paragraph
    top = qi if q else first
    rule = next((i for i in range(top - 1, -1, -1) if re.match(r"^\s*[─━-]{8,}\s*$", lines[i])), max(0, top - 14))
    detail, tip = [], False
    for l in lines[rule + 1:top]:
        if ART.search(l):                                 # the welcome banner of the first-run screens
            continue
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


# ── Claude's own input box, and the screens that come before the first prompt ─────────────────
RULE = re.compile(r"^─{20,}\s*$")
SGR = re.compile(r"\x1b\[[0-9;:]*m")
ART = re.compile(r"[░▒▓█▄▀▐▌▛▜▝▘▗▖]")
CURSOR = re.compile(r"^(\s*)❯[\s ]+(?:(\d)\.\s+)?(.+?)\s*$")


def draft(sess, cap_e=None):
    """What sits in Claude's input box and was NOT sent: typed in Term, or put back there by Esc
    (Esc on a message Claude hadn't answered yet returns it to the box, yet the transcript keeps it,
    so the chat would show it as sent). None = no input box on screen (a dialog, setup, another UI)."""
    cap_e = cap_e if cap_e is not None else _run(TMUX + ["capture-pane", "-p", "-e", "-t", f"={sess}:"])
    if cap_e is not None and not isinstance(cap_e, str):
        cap_e = cap_e.stdout if cap_e.returncode == 0 else None
    if not cap_e:
        return None
    raw = cap_e.splitlines()
    plain = [SGR.sub("", l) for l in raw]
    box = [i for i in range(1, len(plain)) if RULE.match(plain[i - 1]) and re.match(r"^❯[\s ]", plain[i])]
    if not box:
        return None
    i = box[-1]
    # a dimmed placeholder / suggestion is not text you typed
    m = re.match(r"^(?:\x1b\[[0-9;:]*m)*❯[\s ]*((?:\x1b\[[0-9;:]*m)*)", raw[i])
    if m and re.search(r"\[(?:2|90|38;5;(?:2[3-4]\d|8))m", m.group(1)):
        return ""
    out = [plain[i][2:].rstrip()]
    for l in plain[i + 1:]:
        if RULE.match(l):
            break
        out.append(l[2:].rstrip())
    return "\n".join(out).strip("\n").strip()


def _menu(lines):
    """A selection list with Claude's ❯ cursor, numbered or not ('❯ No, exit / Yes, I trust this
    folder'). → (options, index of the cursor) or None."""
    ci = next((i for i in range(len(lines) - 1, -1, -1) if CURSOR.match(lines[i])), None)
    if ci is None:
        return None
    cm = CURSOR.match(lines[ci])
    numbered = bool(cm.group(2))
    col = len(cm.group(1)) + 2
    item = (lambda l: OPT.match(l)) if numbered else \
           (lambda l: CURSOR.match(l) or (len(l) - len(l.lstrip()) == col and not re.match(r"^\s*(Esc|Enter|Tab|Press)\b", l)))
    a = ci
    while a > 0 and item(lines[a - 1]):
        a -= 1
    b = ci
    while b + 1 < len(lines) and item(lines[b + 1]):
        b += 1
    opts = []
    for l in lines[a:b + 1]:
        m = CURSOR.match(l) or OPT.match(l)
        label = (m.group(3) if m and m.re is CURSOR else m.group(2) if m else l.strip()).replace("✔", "").strip()
        opts.append({"n": str(len(opts) + 1), "label": label[:120]})
    return (opts, ci - a, a) if len(opts) >= 2 else None


def setup_screen(sess, cap=None):
    """Claude is running but not at its prompt and not in a permission box: the first-run screens
    (theme, login method, the sign-in link and its code, trust this folder, Press Enter…). Shown as
    they are, so the chat never sits on 'starting…' while Claude waits for an answer."""
    cap = cap if cap is not None else _capture(sess)
    if not cap:
        return None
    lines = [l.replace("│", " ").rstrip() for l in cap.splitlines()]
    lines = [l for l in lines if l.strip() and not ART.search(l) and not re.match(r"^[\s.╌─]+$", l)]
    if not lines:
        return None
    # the sign-in URL is hard-wrapped by the UI: join its pieces back
    url, j = "", next((k for k, l in enumerate(lines) if l.startswith("https://")), None)
    if j is not None:
        k = j
        while k < len(lines) and re.match(r"^\S+$", lines[k]):
            url += lines[k]
            k += 1
        lines = lines[:j] + ["(sign-in link below)"] + lines[k:]
    menu = _menu(lines)
    opts, cur, top = menu if menu else ([], 0, len(lines))
    if not menu and not url and not re.search(r"Press Enter|Paste code|Enter to|to continue", cap):
        return None       # a screen in transition: nothing to answer yet
    above = [l.strip() for l in lines[max(0, top - 8):top]]
    q = above.pop() if menu and above and above[-1].endswith(("?", ":")) else ""
    detail = "\n".join(above)[-1200:]
    return {"setup": True, "question": q[:200], "opts": opts[:9], "cur": cur, "detail": detail[:1500],
            "url": url[:3000], "code": bool(re.search(r"Paste code", cap))}


def pick(sess, allowed, n):
    """Choose option n of the list on screen: arrows from where the cursor is + Enter (numbers
    aren't always there, and not every list takes them)."""
    if sess not in allowed or not str(n).isdigit():
        return False
    cap = _capture(sess)
    lines = [l.replace("│", " ").rstrip() for l in (cap or "").splitlines() if l.strip()]
    menu = _menu([l for l in lines if not ART.search(l)])
    if not menu or not 1 <= int(n) <= len(menu[0]):
        return False
    d = int(n) - 1 - menu[1]
    keys = ["Down" if d > 0 else "Up"] * abs(d) + ["Enter"]
    r = _run(TMUX + ["send-keys", "-t", f"={sess}:"] + keys)
    return bool(r and r.returncode == 0)


def clear_draft(sess):
    """Empty the input box (Esc Esc = Claude Code's 'clear'). Only when there IS text and Claude isn't
    working: on an empty box the double Esc opens the rewind menu, and while working it interrupts."""
    if not draft(sess) or spinner(sess):
        return False
    tgt = f"={sess}:"
    _run(TMUX + ["send-keys", "-t", tgt, "Escape"])
    time.sleep(0.3)
    _run(TMUX + ["send-keys", "-t", tgt, "Escape"])
    time.sleep(0.3)
    return draft(sess) == ""


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
    scr, spin, dr = screen(sess, cap), spinner(sess, cap), draft(sess)
    if scr is None and dr is None and not spin:
        scr = setup_screen(sess)
    res = {"s": sess, "screen": scr, "spin": spin, "draft": dr or ""}
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


def send(sess, allowed, text=None, key=None, choose=None, clear=False):
    if sess not in allowed:
        return False
    tgt = f"={sess}:"
    if choose:
        return pick(sess, allowed, choose)
    if clear:
        return clear_draft(sess)
    if key:
        if key not in KEYS:
            return False
        r = _run(TMUX + ["send-keys", "-t", tgt, key])
        return bool(r and r.returncode == 0)
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return False
    if draft(sess):          # text left in the box (Esc put a message back): replace it, don't glue onto it
        clear_draft(sess)
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
