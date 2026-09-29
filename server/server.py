#!/usr/bin/env python3
"""
controlroom server — one process, Python standard library only.

It serves everything the browser needs from ONE origin, so the page can reach into the
terminal iframes (on-screen keys, touch scrolling, clipboard bridge):

  /               the grid UI (web/index.html)
  /tty/...        reverse proxy to ttyd (HTTP + WebSocket), which listens on a UNIX socket
                  and therefore has no TCP port of its own
  /api/status     per-session state (from the Claude Code hook) + slog locks + recent feed
  /clip.txt       text an agent left for you to copy (bin/cr-clip); /clip-1..3.txt = the previous ones
  /files, /dl     browse and download files (starts in the outbox, see bin/cr-expose)
  POST /upload    save files, optionally shrink them for tokens, type the path into a session
  POST /rm        delete a file — only inside the uploads/ and outbox/ folders
  POST /stt       dictation: audio -> Whisper -> text typed into a session's prompt (no Enter)

!!! SECURITY: this server has NO authentication. Anyone who can reach it gets a shell as
!!! your user. It binds to 127.0.0.1 by default. Put it behind real auth before exposing it
!!! (see README, "Security"). Do not change CR_BIND to 0.0.0.0 on a public network.

Why forms and not fetch() for uploads/dictation: some auth proxies (e.g. Cloudflare) answer
background XHR/fetch POSTs with an interactive challenge page, which silently kills them.
A normal form POST that targets an iframe is a navigation, so it passes. Keep that pattern.
"""
import html, json, mimetypes, os, re, select, shutil, socket, subprocess, sys, time
import urllib.error, urllib.request
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import formatdate
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat   # Chat view per tile + plan usage

# ── Configuration (all from the environment; start.sh loads controlroom.env) ────────────
HERE      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E         = os.environ.get
STATE_DIR = E("CR_STATE_DIR") or os.path.join(E("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "controlroom")
WORKDIR   = os.path.expanduser(E("CR_WORKDIR") or "~")
# Uploads live INSIDE the working directory on purpose: Claude Code asks for permission to
# read files outside its cwd, so an upload elsewhere would trigger a prompt every time.
DATA_DIR  = os.path.expanduser(E("CR_DATA_DIR") or os.path.join(WORKDIR, ".controlroom"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
OUTBOX     = os.path.expanduser(E("CR_OUTBOX") or os.path.join(DATA_DIR, "outbox"))
STT_LOG    = os.path.join(DATA_DIR, "dictations")
BOARD      = E("SLOG_BOARD") or os.path.join(STATE_DIR, "SESSIONS.md")
SLOG       = os.path.join(HERE, "bin", "slog")
WEB_DIR    = os.path.join(HERE, "web")
BIND       = (E("CR_BIND") or "127.0.0.1", int(E("CR_PORT") or "7680"))
TTYD_SOCK  = E("CR_TTYD_SOCKET") or os.path.join(STATE_DIR, "ttyd.sock")
SESSIONS   = (E("CR_SESSIONS") or "claude1 claude2 claude3 claude4 shell").split()
SHELLS     = (E("CR_SHELL_SESSIONS") or "shell").split()
TMUX       = ["tmux"] + (["-L", E("CR_TMUX_SOCKET")] if E("CR_TMUX_SOCKET") else [])
VIEW_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}

# Browsable roots (realpath). Anything outside -> 404, even if it exists.
ALLOWED_ROOTS = [os.path.realpath(os.path.expanduser(p)) for p in
                 (E("CR_FILE_ROOTS") or ":".join([WORKDIR, DATA_DIR, OUTBOX])).split(":") if p]
# Deletable = only what the control room itself creates.
DELETE_ROOTS = [os.path.realpath(p) for p in (UPLOAD_DIR, OUTBOX)]
chat.init(TMUX, STATE_DIR, ALLOWED_ROOTS)

MAX_BODY  = 200 * 1024 * 1024
MAX_LIST  = 300
SESS_RE   = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
IMG_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp", ".tiff"}
MAGICK    = shutil.which("magick") or shutil.which("convert")      # optional
PDFTOTEXT = shutil.which("pdftotext")                               # optional (poppler-utils)
PDFINFO   = shutil.which("pdfinfo")

# ── Dictation (optional) ───────────────────────────────────────────────────────────────────
# CR_STT_PROVIDER = openai | azure | (empty = the 🎤 button is hidden)
STT_PROVIDER = (E("CR_STT_PROVIDER") or "").lower()
STT_LANG     = E("CR_STT_LANGUAGE") or ""          # e.g. "en", "es"; empty = auto-detect
# Whisper's `prompt` biases spelling toward your jargon ("Postgres" instead of "post grass").
# KEEP IT SHORT AND ONLY PROPER NOUNS. It is not an instruction: it is fed to the decoder as
# preceding context, so every extra token costs accuracy. Measured on the same 5 s clip:
# 0-113 chars of names -> correct; a 269-char prompt that opened with explanatory prose ->
# real words started coming out wrong. Words Whisper already knows add nothing.
STT_PROMPT   = E("CR_STT_PROMPT") or ""
MAX_AUDIO    = 24 * 1024 * 1024                    # the APIs cap uploads at 25 MB
MIN_AUDIO    = 1500                                # smaller = an accidental tap, not speech
STT_KEEP     = 12                                  # keep the last N dictations for auditing

# Whisper "hallucinates" on silence/noise: it returns fluent, plausible text such as
# "Thanks for watching!" or "Subtitles by the Amara.org community". Looking at the string
# cannot catch a NEW hallucination, so the real filter is per-segment METRICS from
# response_format=verbose_json. Thresholds measured on real traffic (not copied from a blog):
#   real speech ................. no_speech_prob 0.00–0.18
#   3 s of silence / noise / tone no_speech_prob 0.75–0.95  (all three returned Amara.org text)
# avg_logprob does NOT separate them (the hallucination is confident, well-formed text);
# it and compression_ratio catch the other two failure modes: mumbling and repetition loops.
STT_NO_SPEECH = float(E("CR_STT_NO_SPEECH") or 0.60)
STT_LOGPROB   = float(E("CR_STT_LOGPROB") or -1.00)
STT_COMPRESS  = float(E("CR_STT_COMPRESS") or 2.40)
# Last-resort blocklist of known hallucinations (several languages).
JUNK_RE = re.compile(r"^[\s.,!?¡¿·\-]*$|amara\.org|subt[ií]tulos? (realizados|por|creados)|"
                     r"^\W*(thanks|thank you) for watching|^\W*please subscribe|"
                     r"^¡?gracias por ver|^¡?suscr[ií]b|untertitel (im auftrag|von)|"
                     r"^\W*(ah|eh|uh|um+|mm+|hm+)\W*$", re.I)
# Sign-offs you COULD really say ("that's all for today") are dropped only if the metrics are
# also weak — seen in the wild: 5 s of silence -> a full "see you in the next video" line with
# no_speech_prob 0.26, which no single threshold catches.
SOSP_RE = re.compile(r"^\W*(that'?s all for today|see you (next time|in the next)|"
                     r"eso es todo por|nos vemos|hasta la pr[oó]xima)|don'?t forget to subscribe", re.I)


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def sane_name(name):
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return name[:120]


def run(cmd, data=None, timeout=90):
    try:
        return subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except Exception:
        return None


def optimize_image(raw, ext):
    """Shrink an image for the model: <=1568 px on the long side (the size vision models
    downscale to anyway), metadata stripped, JPEG q85. PNG stays PNG — screenshots with text
    get JPEG artifacts. Returns (bytes, ext, note); the original if it would not help."""
    if not MAGICK:
        return raw, ext, "not optimized (ImageMagick not installed)"
    keep_png = ext == ".png"
    cmd = [MAGICK, "-", "-auto-orient", "-resize", "1568x1568>", "-strip"]
    if not keep_png:
        cmd += ["-quality", "85"]
    r = run(cmd + ["png:-" if keep_png else "jpg:-"], data=raw)
    if not r or r.returncode != 0 or not r.stdout:
        return raw, ext, "not optimized (convert failed)"
    if len(r.stdout) >= len(raw):
        return raw, ext, "already small"
    return r.stdout, (".png" if keep_png else ".jpg"), f"{human(len(raw))} → {human(len(r.stdout))}"


def process_pdf(path, optimize):
    """Page count + a .txt sidecar: reading text costs far fewer tokens than the PDF."""
    notes = []
    if PDFINFO:
        r = run([PDFINFO, path])
        if r and r.returncode == 0:
            m = re.search(rb"Pages:\s+(\d+)", r.stdout)
            if m:
                notes.append(f"{int(m.group(1))} pages")
    if optimize and PDFTOTEXT:
        txt = path + ".txt"
        r = run([PDFTOTEXT, "-layout", path, txt])
        if r and r.returncode == 0 and os.path.exists(txt):
            notes.append(f"text extracted: {os.path.basename(txt)} ({human(os.path.getsize(txt))}) — read the .txt to save tokens")
    return notes


def type_into_session(sess, text):
    """Type text into the session's prompt WITHOUT pressing Enter — you review, then send."""
    if not SESS_RE.match(sess) or sess not in SESSIONS:
        return False
    r = run(TMUX + ["has-session", "-t", "=" + sess])
    if not r or r.returncode != 0:
        return False
    # send-keys targets a PANE: "=sess" alone fails with "can't find pane"; "=sess:" is
    # "exact session name, its active pane". -l = literal (no key-name interpretation).
    r = run(TMUX + ["send-keys", "-t", "=" + sess + ":", "-l", text])
    return bool(r and r.returncode == 0)


def one_line(text):
    """Claude Code's prompt is one line: a newline sent with send-keys would SUBMIT it before
    you could review. Collapse line breaks and control characters first."""
    text = re.sub(r"[\r\n\t]+", " ", text or "")
    text = "".join(c for c in text if c == " " or (c >= " " and c != "\x7f"))
    return re.sub(r"\s{2,}", " ", text).strip()


def in_roots(path):
    """(ok, realpath) — ok only if the resolved path is INSIDE an allowed root.
    realpath first, so '../' tricks and symlinks pointing outside are rejected."""
    if not path or not str(path).strip():
        return False, ""
    try:
        rp = os.path.realpath(os.path.expanduser(path))
    except Exception:
        return False, ""
    return any(rp == r or rp.startswith(r + os.sep) for r in ALLOWED_ROOTS), rp


def in_delete_roots(path):
    """Strictly inside uploads/ or outbox/ (the folders themselves cannot be deleted)."""
    rp = os.path.realpath(path)
    return any(rp.startswith(r + os.sep) for r in DELETE_ROOTS)


# ── Status: tmux + hook state + slog ──────────────────────────────────────────────────────
def live_sessions():
    r = run(TMUX + ["list-sessions", "-F", "#S"], timeout=5)
    if not r or r.returncode != 0:
        return set()
    return set(r.stdout.decode(errors="replace").split())


def read_state(sess):
    try:
        with open(os.path.join(STATE_DIR, "state", sess + ".json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


# Leaving "blocked" when you answer NO. The hook enters "blocked" (Notification
# permission_prompt) and a "yes" leaves it (the tool runs → PostToolUse), but a "no", Esc or ^C
# fires no hook, so the tile stayed red until your next prompt. The screen fills that gap:
# once we have SEEN the permission box for this blocked episode, its disappearance means the
# question was answered. The screen is only ever used to LEAVE "blocked", never to enter it, and
# only after it confirmed the box once — so if a Claude Code UI update breaks these patterns,
# the box is simply never "seen" and you are back to hook-only behavior, never a missed alert.
PERMISSION_BOX = re.compile(r"Do you want to proceed\?|No, and tell Claude what to do|Do you trust the files|❯ 1\.")
_box_seen = {}           # session -> ts of the hook "blocked" event whose box we saw on screen


def pane_shows_permission(sess, pane=""):
    """True/False = the box is/isn't on screen; None = couldn't read the pane."""
    target = pane if re.fullmatch(r"%\d+", pane or "") else "=" + sess + ":"
    r = run(TMUX + ["capture-pane", "-p", "-t", target], timeout=3)
    if not r or r.returncode != 0:
        return None
    return bool(PERMISSION_BOX.search(r.stdout.decode(errors="replace")))


def effective_state(sess):
    s = read_state(sess)
    st = s.get("state") or "unknown"      # no hook event yet (or hooks not installed)
    if st != "blocked":
        return st
    on_screen = pane_shows_permission(sess, s.get("pane", ""))
    if on_screen:
        _box_seen[sess] = s.get("ts", 0)
    elif on_screen is False and _box_seen.get(sess) == s.get("ts", 0):
        return "idle"                     # answered where no hook can see it: "no", Esc, ^C
    return st


def slog_locks():
    r = run([SLOG, "locks"], timeout=10)
    out = []
    if r and r.returncode == 0:
        for line in r.stdout.decode(errors="replace").splitlines():
            p = line.split("\t")
            if len(p) >= 3:
                out.append({"key": p[0], "owner": p[1], "since": p[2], "stale": len(p) > 3 and p[3] == "stale"})
    return sorted(out, key=lambda x: x["key"])


def slog_feed(n=15):
    try:
        with open(BOARD, encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip("\n") for l in fh if l.strip() and not l.startswith("#")]
    except Exception:
        return []
    out = []
    for l in lines[-n:]:
        l = re.sub(r" · s:[0-9a-f-]{8,}$", "", l)        # the transcript stamp is for tracing, not display
        p = l.split(" · ", 2)
        out.append({"ts": p[0], "who": p[1], "msg": p[2]} if len(p) == 3 else {"ts": "", "who": "", "msg": l})
    return out


def status():
    live = live_sessions()
    locks = slog_locks()
    sessions = []
    for s in SESSIONS:
        if s not in live:
            st = "off"
        elif s in SHELLS:
            st = "shell"
        else:
            st = effective_state(s)
        sessions.append({"name": s, "state": st, "mode": chat.pane_mode(s) if st not in ("off", "shell") else "",
                         "locks": [l["key"] for l in locks if l["owner"] == s]})
    return {"sessions": sessions, "locks": locks, "feed": slog_feed(),
            "blocked": [x["name"] for x in sessions if x["state"] == "blocked"],
            "stt": STT_PROVIDER in ("openai", "azure"), "ts": int(time.time())}


# ── Dictation ─────────────────────────────────────────────────────────────────────────────
def build_multipart(fields, fname, fdata, fctype):
    boundary = "----controlroom" + os.urandom(9).hex()
    chunks = []
    for k, v in fields.items():
        if v == "":
            continue
        chunks.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                       % (boundary, k, v)).encode())
    chunks.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                   'Content-Type: %s\r\n\r\n' % (boundary, fname, fctype)).encode())
    chunks.append(fdata)
    chunks.append(('\r\n--%s--\r\n' % boundary).encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def stt_filter(data):
    """(text, dropped) — remove HALLUCINATED segments, keep the real ones.
    Per segment, not per transcript, so a good dictation with a noisy tail survives."""
    segs = data.get("segments") or []
    if not segs:                                    # no verbose_json: only the blocklist is left
        txt = (data.get("text") or "").strip()
        return ("", ["no-segments"]) if JUNK_RE.search(txt) else (txt, [])
    good, dropped = [], []
    for s in segs:
        t = (s.get("text") or "").strip()
        nsp = s.get("no_speech_prob", 0.0)
        alp = s.get("avg_logprob", 0.0)
        ctr = s.get("compression_ratio", 0.0)
        why = ""
        if nsp > STT_NO_SPEECH:
            why = "no speech (nsp=%.2f)" % nsp
        elif alp < STT_LOGPROB:
            why = "low confidence (alp=%.2f)" % alp
        elif ctr > STT_COMPRESS:
            why = "repetition loop (ctr=%.2f)" % ctr
        elif JUNK_RE.search(t):
            why = "known hallucination"
        elif SOSP_RE.search(t) and (alp < -0.5 or nsp > 0.35):
            why = "video sign-off (alp=%.2f nsp=%.2f)" % (alp, nsp)
        if why:
            dropped.append("%r → %s" % (t[:60], why))
        else:
            good.append(t)
    return " ".join(x for x in good if x).strip(), dropped


def stt_save(raw, fname, meta):
    """Keep the last STT_KEEP dictations (audio + metrics + text). Without the audio,
    'it misheard me' cannot be audited. Never lets logging break a dictation."""
    try:
        os.makedirs(STT_LOG, exist_ok=True)
        base = os.path.join(STT_LOG, time.strftime("%Y%m%d-%H%M%S"))
        with open(base + (os.path.splitext(fname)[1] or ".webm"), "wb") as fh:
            fh.write(raw)
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        for old in sorted(f for f in os.listdir(STT_LOG) if f.endswith(".json"))[:-STT_KEEP]:
            for f in os.listdir(STT_LOG):
                if f.startswith(old[:-5]):
                    try:
                        os.remove(os.path.join(STT_LOG, f))
                    except OSError:
                        pass
    except Exception:
        pass


def stt_request():
    """(url, headers, model_field) for the configured provider, or raise ValueError."""
    if STT_PROVIDER == "openai":
        key = E("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY is not set")
        # whisper-1 is the model that returns verbose_json with per-segment metrics
        return ("https://api.openai.com/v1/audio/transcriptions",
                {"Authorization": "Bearer " + key}, E("CR_STT_MODEL") or "whisper-1")
    if STT_PROVIDER == "azure":
        endpoint = (E("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")   # https://<resource>.openai.azure.com
        key = E("AZURE_OPENAI_API_KEY")
        dep = E("AZURE_WHISPER_DEPLOYMENT") or "whisper"
        ver = E("AZURE_OPENAI_API_VERSION") or "2024-06-01"
        if not endpoint or not key:
            raise ValueError("AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY are not set")
        return ("%s/openai/deployments/%s/audio/transcriptions?api-version=%s" % (endpoint, dep, ver),
                {"api-key": key}, "")
    raise ValueError("dictation is disabled (set CR_STT_PROVIDER)")


def transcribe(raw, fname, ctype):
    """(text, error, meta). The API key only ever travels in a request header; it is never
    logged or written to the dictation log."""
    meta = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "file": fname, "bytes": len(raw),
            "ctype": ctype, "provider": STT_PROVIDER}
    try:
        url, headers, model = stt_request()
    except ValueError as e:
        return "", str(e), meta
    body, ct = build_multipart({"model": model, "language": STT_LANG, "prompt": STT_PROMPT,
                                "response_format": "verbose_json"}, fname, raw, ctype)
    headers = dict(headers, **{"Content-Type": ct})

    data = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                detail = ""
            # 429 = the provider's per-minute rate limit (common on small tiers when you
            # re-dictate quickly), not a problem with your audio. Retry ONCE, honoring Retry-After.
            if e.code == 429 and attempt == 1:
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except Exception:
                    wait = 0
                if not wait:
                    m = re.search(r"retry after (\d+)", detail, re.I)
                    wait = int(m.group(1)) if m else 15
                if wait <= 30:
                    time.sleep(wait + 1)
                    continue
                meta["error"] = "429 retry-after %ss" % wait
                return "", ("Rate limited by the provider (%s s). That is the per-minute quota, "
                            "not your audio — wait and try again." % wait), meta
            meta["error"] = "HTTP %s" % e.code
            return "", "HTTP %s %s" % (e.code, detail), meta
        except Exception as e:
            meta["error"] = type(e).__name__
            return "", "%s: %s" % (type(e).__name__, e), meta
    if data is None:
        meta["error"] = "no response"
        return "", "The provider returned nothing — try again.", meta

    text, dropped = stt_filter(data)
    meta.update(duration=data.get("duration"), language=data.get("language"),
                raw=(data.get("text") or "").strip(), text=text, dropped=dropped,
                segments=[{k: s.get(k) for k in ("start", "end", "text", "no_speech_prob",
                                                 "avg_logprob", "compression_ratio")}
                          for s in (data.get("segments") or [])])
    stt_save(raw, fname, meta)
    return text, "", meta


# ── ttyd reverse proxy ────────────────────────────────────────────────────────────────────
def ttyd_connect():
    if ":" in TTYD_SOCK and not TTYD_SOCK.startswith("/"):       # "host:port" also works
        host, port = TTYD_SOCK.rsplit(":", 1)
        return socket.create_connection((host, int(port)), timeout=10)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(TTYD_SOCK)
    return s


def splice(a, b):
    """Copy bytes both ways until either side closes (used for the WebSocket)."""
    a.settimeout(None)
    b.settimeout(None)
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x:
                return
            if not r:
                continue                 # idle; ttyd sends its own keep-alives
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


# ── HTML shell for the small server-rendered pages (upload result, file list, dictation) ──
PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{{margin:0;padding:10px 12px;background:#262624;color:#ece9e1;
      font:13.5px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
 code,pre,.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}}
 .ok{{color:#9fca87}} .warn{{color:#d97757}} .muted{{color:#a19f97}}
 code{{background:#1f1e1d;border:1px solid #3a3935;border-radius:5px;padding:1px 5px;word-break:break-all}}
 ul{{margin:6px 0;padding-left:18px}} li{{margin:4px 0}}
 a{{color:#e3a27f;text-decoration:none}} a:hover{{text-decoration:underline}}
 .crumb{{color:#a19f97;font-size:11.5px;word-break:break-all;margin:0 0 8px}}
 .quick{{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}}
 .quick a{{background:#2d2c29;border:1px solid #3a3935;border-radius:7px;padding:6px 10px;color:#ece9e1;font-size:12px}}
 .quick a.on{{border-color:#d97757;color:#d97757}}
 .row{{display:flex;align-items:center;gap:9px;padding:8px 2px;border-bottom:1px solid #34322f}}
 .row:last-child{{border-bottom:0}}
 .row .nm{{flex:1;min-width:0;overflow-wrap:anywhere}}
 .row .meta{{color:#a19f97;font-size:11px;white-space:nowrap}}
 .dl{{flex:0 0 auto;background:#d97757;color:#fff;font-weight:700;border-radius:7px;padding:8px 13px}}
 .dl:hover{{text-decoration:none;opacity:.9}}
 form.br{{display:flex;gap:6px;margin-top:12px}}
 form.br input{{flex:1;min-width:0;background:#1f1e1d;border:1px solid #3a3935;color:#ece9e1;
               border-radius:7px;padding:9px;font:inherit;font-size:16px}}
 form.br button{{background:#2d2c29;border:1px solid #3a3935;color:#ece9e1;border-radius:7px;padding:9px 12px;font:inherit;cursor:pointer}}
 form.rmf{{display:inline;margin:0;flex:0 0 auto}}
 .rm{{background:#2d2c29;border:1px solid #5a3a33;color:#e5786d;border-radius:7px;padding:8px 11px;font:inherit;font-size:12px;cursor:pointer}}
 .rm:hover{{border-color:#e5786d}}
 .note{{background:#26302a;border:1px solid #3d5a44;border-radius:8px;padding:8px 10px;margin:0 0 10px}}
 .note.bad{{background:#33241f;border-color:#6a3a33}}
 .said{{background:#1f1e1d;border:1px solid #3a3935;border-radius:8px;padding:10px 12px;font-size:14px;line-height:1.55;color:#f5f1e8;margin:0 0 8px}}
 @media (max-width:640px){{ .row{{flex-wrap:wrap;gap:6px 9px}} .row .nm{{flex:1 0 100%}} .row .meta{{margin-right:auto}} }}
</style></head><body{attrs}>{body}</body></html>"""


def rm_form(name, path, back_dir, back=""):
    """'delete' button: a navigational form POST + the browser's confirm(). Empty outside
    uploads/ and outbox/ — there the button simply does not exist."""
    if not in_delete_roots(path):
        return ""
    # the name goes inside a single-quoted JS string: escape \ and ' BEFORE html-escaping
    js = html.escape(name.replace("\\", "\\\\").replace("'", "\\'"))
    return ("<form class='rmf' method='POST' action='rm' "
            f"onsubmit=\"return confirm('Delete {js}?')\">"
            f"<input type='hidden' name='p' value='{html.escape(path)}'>"
            f"<input type='hidden' name='d' value='{html.escape(back_dir)}'>"
            + (f"<input type='hidden' name='back' value='{html.escape(back)}'>" if back else "")
            + "<button class='rm' type='submit'>delete</button></form>")


def when(ts):
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%m-%d %H:%M")


class Handler(SimpleHTTPRequestHandler):
    _head = False
    server_version = "controlroom"
    sys_version = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        # The UI is meant to be framed only by itself.
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _page(self, code, body, ok=False):
        # data-ok lets the (same-origin) UI auto-close the dictation dialog on success
        data = PAGE.format(body=body, attrs=' data-ok="1"' if ok else "").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not self._head:
            self.wfile.write(data)

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not self._head:
            self.wfile.write(data)

    def _text_file(self, path):
        mtime = None
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            mtime = os.path.getmtime(path)
        except OSError:
            data = b""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        if mtime:                                        # the clip history (⌄) shows when each one was loaded
            self.send_header("Last-Modified", formatdate(mtime, usegmt=True))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not self._head:
            self.wfile.write(data)

    # ── routing ───────────────────────────────────────────────────────────────────────
    def _is_tty(self):
        p = urlsplit(self.path).path
        return p == "/tty" or p.startswith("/tty/")

    def _route(self):
        parts = urlsplit(self.path)
        route = parts.path.rstrip("/") or "/"
        q = parse_qs(parts.query)
        if route == "/api/status":
            self._json(status()); return True
        if route in ("/clip.txt", "/clip-1.txt", "/clip-2.txt", "/clip-3.txt"):
            self._text_file(os.path.join(STATE_DIR, route[1:])); return True
        if route == "/files":
            self.page_files(q); return True
        if route == "/dl":
            self.send_download(q); return True
        if route == "/view":
            self.send_view(q); return True
        if route == "/api/chat":
            try:
                off = int((q.get("off") or ["0"])[0])
            except ValueError:
                off = 0
            self._json(chat.get((q.get("s") or [""])[0], (q.get("sid") or [""])[0], off, SESSIONS)); return True
        if route == "/api/usage":
            self._json(chat.usage()); return True
        return False

    def do_GET(self):
        if self._is_tty():
            return self.proxy_ttyd()
        if not self._route():
            super().do_GET()

    def do_HEAD(self):
        self._head = True
        try:
            if not self._route():
                super().do_HEAD()
        finally:
            self._head = False

    def do_POST(self):
        if self._is_tty():
            return self.proxy_ttyd()
        route = urlsplit(self.path).path.rstrip("/") or "/"
        fn = {"/upload": self.handle_upload, "/rm": self.handle_rm, "/stt": self.handle_stt,
              "/chat/send": self.handle_chat_send}.get(route)
        if not fn:
            return self._page(404, "<p class='warn'>404</p>")
        try:
            fn()
        except Exception as e:
            self._page(500, f"<p class='warn'>✗ error: {html.escape(str(e))}</p>")

    # ── /tty → ttyd ───────────────────────────────────────────────────────────────────
    def proxy_ttyd(self):
        """Forward the request to ttyd as-is. For a WebSocket upgrade, keep both sockets
        spliced together for the life of the terminal. For plain HTTP, force
        'Connection: close' so a kept-alive browser connection can't carry the NEXT request
        (which may not be /tty) straight to ttyd."""
        try:
            up = ttyd_connect()
        except OSError as e:
            return self._page(502, f"<p class='warn'>✗ ttyd is not reachable ({html.escape(str(e))}) — is start.sh running?</p>")
        upgrade = (self.headers.get("Upgrade") or "").lower() == "websocket"
        lines = [self.requestline]
        for k, v in self.headers.items():
            if not upgrade and k.lower() in ("connection", "keep-alive", "proxy-connection"):
                continue
            lines.append(f"{k}: {v}")
        if not upgrade:
            lines.append("Connection: close")
        try:
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                up.sendall(self.rfile.read(n))
            if upgrade:
                splice(self.connection, up)
            else:
                up.settimeout(60)
                while True:
                    chunk = up.recv(65536)
                    if not chunk:
                        break
                    self.connection.sendall(chunk)
        except OSError:
            pass
        finally:
            up.close()
            self.close_connection = True

    # ── 📥 browse / download ──────────────────────────────────────────────────────────
    def page_files(self, q, notice=""):
        ok, d = in_roots((q.get("d") or [""])[0] or OUTBOX)
        if not ok or not os.path.isdir(d):
            return self._page(404, "<p class='warn'>✗ folder is outside the allowed roots or does not exist</p>"
                                   f"<p class='muted'>allowed: {html.escape(', '.join(ALLOWED_ROOTS))}</p>")
        quick = [("📤 outbox", OUTBOX), ("📎 uploads", UPLOAD_DIR), ("🎤 dictations", STT_LOG),
                 ("workdir", WORKDIR)]
        chips = ""
        for lbl, p in quick:
            if not os.path.isdir(p):
                continue
            cls = " class='on'" if os.path.realpath(p) == d else ""
            chips += "<a href='files?d=%s'%s>%s</a>" % (quote(p), cls, html.escape(lbl))
        try:
            names = os.listdir(d)
        except Exception as e:
            return self._page(500, f"<p class='warn'>✗ cannot read: {html.escape(str(e))}</p>")
        dirs, files = [], []
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except Exception:
                continue
            (dirs if os.path.isdir(p) else files).append((n, p, st))
        dirs.sort(key=lambda t: t[0].lower())
        files.sort(key=lambda t: t[2].st_mtime, reverse=True)      # newest first

        rows = []
        parent = os.path.dirname(d)
        if in_roots(parent)[0] and parent != d:
            rows.append(f"<div class='row'><span class='nm'><a href='files?d={quote(parent)}'>⬑ ..</a></span></div>")
        for n, p, st in dirs[:MAX_LIST]:
            rows.append(f"<div class='row'><span class='nm'>📁 <a href='files?d={quote(p)}'>{html.escape(n)}/</a></span>"
                        f"{rm_form(n, p, d)}</div>")
        for n, p, st in files[:MAX_LIST]:
            # "download" as text, not a glyph: exotic arrows render as empty boxes on many phones
            rows.append(f"<div class='row'><span class='nm'>{html.escape(n)}</span>"
                        f"<span class='meta'>{human(st.st_size)} · {when(st.st_mtime)}</span>"
                        f"{rm_form(n, p, d)}"
                        f"<a class='dl' href='dl?p={quote(p)}' download target='_blank' rel='noopener'>download</a></div>")
        if not dirs and not files:
            rows.append("<p class='muted'>(empty) — from a session: <code>cr-expose &lt;file&gt;</code> puts it here.</p>")
        cut = f"<p class='muted'>… list truncated to {MAX_LIST} per type</p>" if len(dirs) > MAX_LIST or len(files) > MAX_LIST else ""
        head = f"<div class='note{'' if 'ok' in notice else ' bad'}'>{notice}</div>" if notice else ""
        self._page(200, f"{head}<div class='quick'>{chips}</div><p class='crumb'>{html.escape(d)}</p>"
                        f"{''.join(rows)}{cut}"
                        f"<form class='br' method='GET' action='files'>"
                        f"<input name='d' placeholder='/path/to/folder' value='{html.escape(d)}'>"
                        f"<button type='submit'>go</button></form>")

    def send_download(self, q):
        raw = (q.get("p") or [""])[0]
        ok, path = in_roots(raw)
        if not raw or not ok or not os.path.isfile(path):
            return self._page(404, "<p class='warn'>✗ file not available</p>")
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except Exception as e:
            return self._page(500, f"<p class='warn'>✗ {html.escape(str(e))}</p>")
        with fh:
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            # ASCII fallback + filename* (RFC 5987) for non-ASCII names
            self.send_header("Content-Disposition", 'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                             % (sane_name(name), quote(name)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self._head:
                return
            try:
                shutil.copyfileobj(fh, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass                                    # download cancelled

    def _multipart(self, maxlen):
        length = int(self.headers.get("Content-Length") or 0)
        ctype = self.headers.get("Content-Type") or ""
        if not length:
            return None, "<p class='warn'>✗ empty request</p>"
        if length > maxlen:
            return None, f"<p class='warn'>✗ too large (max {human(maxlen)})</p>"
        if "multipart/form-data" not in ctype:
            return None, "<p class='warn'>✗ expected multipart/form-data</p>"
        body = self.rfile.read(length)
        return BytesParser(policy=policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body), ""

    # ── delete ────────────────────────────────────────────────────────────────────────
    # POST, not a GET link: link prefetchers and security scanners follow GETs.
    # Limited to DELETE_ROOTS, never recursive (a folder is removed only if empty).
    def handle_rm(self):
        length = int(self.headers.get("Content-Length") or 0)
        q = parse_qs(self.rfile.read(min(length, 65536)).decode("utf-8", "replace")) if length else {}
        raw, back, home = ((q.get(k) or [""])[0] for k in ("p", "back", "d"))
        ok, path = in_roots(raw)
        if not ok or not in_delete_roots(path):
            return self._rm_done(back, home, "<p class='warn'>✗ only files inside <code>uploads/</code> "
                                             "and <code>outbox/</code> can be deleted from here</p>")
        name = html.escape(os.path.basename(path))
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
                msg = f"<p class='ok'>✔ folder deleted: {name}</p>"
            else:
                os.remove(path)
                extra = ""
                side = path + ".txt"                   # a PDF's text sidecar goes with it
                if path.lower().endswith(".pdf") and os.path.isfile(side) and in_delete_roots(side):
                    os.remove(side)
                    extra = " (+ its .txt)"
                msg = f"<p class='ok'>✔ deleted: {name}{extra}</p>"
        except OSError as e:
            detail = "folder is not empty" if e.errno == 39 else html.escape(e.strerror or str(e))
            msg = f"<p class='warn'>✗ could not delete {name}: {detail}</p>"
        self._rm_done(back, home or os.path.dirname(path), msg)

    def _rm_done(self, back, home, msg):
        if back == "up":                                 # came from an upload result
            return self._page(200, f"<div class='note'>{msg}</div><p class='muted'>done — you can close this.</p>")
        ok, d = in_roots(home or OUTBOX)
        self.page_files({"d": [d if ok else OUTBOX]}, notice=msg)

    # ── 🎤 dictation ──────────────────────────────────────────────────────────────────
    def handle_stt(self):
        msg, err = self._multipart(MAX_AUDIO)
        if err:
            return self._page(400, err)
        sess, audio, fname, actype = "", None, "audio.webm", "audio/webm"
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "sess":
                sess = (part.get_content() or "").strip()
            elif name == "f":
                fname = sane_name(part.get_filename() or "audio.webm")
                actype = part.get_content_type() or "audio/webm"
                audio = part.get_payload(decode=True)
        if not audio or len(audio) < MIN_AUDIO:
            return self._page(400, "<p class='warn'>🎤 nothing was recorded — hold it a bit longer</p>")
        text, err, meta = transcribe(audio, fname, actype)
        if err:
            return self._page(502, f"<p class='warn'>✗ transcription: {html.escape(err)}</p>")
        text = one_line(text)
        if not text:
            det = ""
            if meta.get("dropped"):                      # say WHAT the filter dropped, or it's a mystery
                det = "<p class='muted'>dropped: " + html.escape(" · ".join(meta["dropped"])[:300]) + "</p>"
            return self._page(200, "<p class='warn'>🎤 no speech detected</p>"
                                   "<p class='muted'>(silence or noise) — try closer to the mic</p>" + det)
        typed = type_into_session(sess, " " + text + " ")
        tail = (f"<p class='ok'>⌨ typed into <b>{html.escape(sess)}</b> — review it and press Enter</p>"
                if typed else
                f"<p class='warn'>⚠ could not type into <b>{html.escape(sess or '?')}</b> — copy it from above</p>")
        if meta.get("dropped"):
            tail += "<p class='muted'>dropped: " + html.escape(" · ".join(meta["dropped"])[:200]) + "</p>"
        self._page(200, f"<div class='said'>🎤 {html.escape(text)}</div>{tail}", ok=typed)

    # ── 📎 upload ─────────────────────────────────────────────────────────────────────
    def save_files(self, files, optimize=False):
        """Save [(name, bytes)] to uploads/<day>/HHMMSS_<name> → [(path, human size, notes)]."""
        destdir = os.path.join(UPLOAD_DIR, datetime.now().strftime("%Y-%m-%d"))
        stamp = datetime.now().strftime("%H%M%S")
        os.makedirs(destdir, exist_ok=True)
        items = []
        for fn, raw in files:
            base, ext = os.path.splitext(sane_name(fn))
            ext = ext.lower()
            notes = []
            if optimize and ext in IMG_EXTS:
                raw, ext, note = optimize_image(raw, ext)
                notes.append(note)
            path = os.path.join(destdir, f"{stamp}_{base}{ext}")
            i = 1
            while os.path.exists(path):
                path = os.path.join(destdir, f"{stamp}_{base}-{i}{ext}")
                i += 1
            with open(path, "wb") as fh:
                fh.write(raw)
            if ext == ".pdf":
                notes += process_pdf(path, optimize)
            items.append((path, human(os.path.getsize(path)), notes))
        return items

    # Chat composer: a navigational form POST into a hidden iframe (auth proxies can kill XHR POSTs).
    # Attachments are saved to uploads/ and Claude receives their paths after the text.
    def handle_chat_send(self):
        if "multipart/form-data" in (self.headers.get("Content-Type") or ""):
            msg, err = self._multipart(MAX_BODY)
            if err:
                return self._page(400, err)
            q, files = {}, []
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "f":
                    fn, payload = part.get_filename(), part.get_payload(decode=True)
                    if fn and payload:
                        files.append((fn, payload))
                elif name:
                    q[name] = [part.get_content() or ""]
        else:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000:
                return self._page(413, "<p class='warn'>message too long</p>")
            q, files = parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True), []
        g = lambda k: (q.get(k) or [""])[0]
        text = g("text").strip()
        if files:
            paths = [p for p, _, _ in self.save_files(files)]
            text = (text + "\n\n" if text else "") + "\n".join(paths)
        ok = chat.send(g("s"), SESSIONS, text=text or None, key=g("key") or None)
        self._page(200 if ok else 400, "<p class='ok'>✔</p>" if ok else "<p class='warn'>could not type into the session</p>", ok=ok)

    # Like /dl but INLINE and only images/PDF: chat thumbnails and the in-page viewer use it.
    def send_view(self, q):
        raw = (q.get("p") or [""])[0]
        ok, path = in_roots(raw)
        ext = os.path.splitext(path or "")[1].lower()
        if not raw or not ok or ext not in VIEW_TYPES or not os.path.isfile(path):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if self._head:
            return
        with open(path, "rb") as fh:
            try:
                shutil.copyfileobj(fh, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_upload(self):
        msg, err = self._multipart(MAX_BODY)
        if err:
            return self._page(400, err)
        sess, optimize, typepath, files = "", False, False, []
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "sess":
                sess = (part.get_content() or "").strip()
            elif name == "optimize":
                optimize = True
            elif name == "typepath":
                typepath = True
            elif name == "f":
                fn, payload = part.get_filename(), part.get_payload(decode=True)
                if fn and payload:
                    files.append((fn, payload))
        if not files:
            return self._page(400, "<p class='warn'>✗ no file received</p>")

        items = self.save_files(files, optimize)
        paths = [p for p, _, _ in items]
        destdir = os.path.dirname(paths[0])

        typed = typepath and sess and type_into_session(sess, " " + " ".join(paths) + " ")
        lis = "".join(
            f"<li><code>{html.escape(p)}</code> <span class='muted'>({sz}"
            + ("; " + "; ".join(html.escape(n) for n in notes) if notes else "") + ")</span> "
            + rm_form(os.path.basename(p), p, destdir, back="up") + "</li>"
            for p, sz, notes in items)
        tail = (f"<p class='ok'>⌨ path typed into <b>{html.escape(sess)}</b> — press Enter when ready</p>" if typed else
                (f"<p class='warn'>⚠ could not type into session {html.escape(sess or '?')} (is it running?) — copy the path</p>"
                 if typepath else "<p class='muted'>path not typed (option unchecked)</p>"))
        self._page(200, f"<p class='ok'>✔ {len(items)} file(s) uploaded</p><ul>{lis}</ul>{tail}")


if __name__ == "__main__":
    for d in (UPLOAD_DIR, OUTBOX, os.path.join(STATE_DIR, "state")):
        os.makedirs(d, exist_ok=True)
    if BIND[0] not in ("127.0.0.1", "::1", "localhost"):
        sys.stderr.write("!!! WARNING: binding to %s — this server has NO authentication. Make sure "
                         "only your auth proxy can reach it.\n" % BIND[0])
    srv = ThreadingHTTPServer(BIND, Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"controlroom on http://{BIND[0]}:{BIND[1]}  (sessions: {' '.join(SESSIONS)}; "
                     f"dictation: {STT_PROVIDER or 'off'})\n")
    srv.serve_forever()
