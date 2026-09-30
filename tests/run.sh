#!/usr/bin/env bash
# tests/run.sh — behavior tests for the coordination pieces. Needs bash, python3, tmux, flock.
# Runs in CI (smoke job) and locally:  bash tests/run.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'tmux -L crtest kill-server 2>/dev/null; tmux -L crchat kill-server 2>/dev/null; rm -rf "$T"' EXIT
export SLOG_BOARD="$T/board/SESSIONS.md" CR_STATE_DIR="$T/state"
unset TMUX TMUX_PANE SLOG_TAG
FAIL=0
ok()  { echo "  ✔ $1"; }
bad() { echo "  ✘ $1"; FAIL=1; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

echo "slog --paths"
mkdir -p "$T/proj/src/auth"
(cd "$T/proj" && SLOG_TAG=alice "$HERE/bin/slog" take "api:auth" --paths 'src/auth/*,README.md' >/dev/null)
locks() { "$HERE/bin/slog" locks > "$T/locks"; }   # to a file: grep -q + pipefail = SIGPIPE flakes
locks; check "lock lists its paths" 'grep -q "$T/proj/src/auth/\*" "$T/locks"'
check "flag column is never empty" 'awk -F"\t" "\$4==\"\"{e=1} END{exit e}" "$T/locks"'
SLOG_TAG=bob "$HERE/bin/slog" take api >"$T/out" 2>&1
check "parent lock warns about the child" 'grep -q OVERLAPS "$T/out"'
locks; check "…and is still recorded" 'grep -q "^api	bob" "$T/locks"'

echo "cr-guard-hook"
guard() { printf '{"tool_name":"%s","tool_input":%s,"cwd":"%s"}' "$2" "$3" "$T/proj" \
          | SLOG_TAG="$1" python3 "$HERE/hooks/cr-guard-hook.py" | grep -q '"deny"'; }
check "Edit on a locked file is denied"        'guard bob Edit "{\"file_path\":\"src/auth/login.py\"}"'
check "…but not for the lock owner"            '! guard alice Edit "{\"file_path\":\"src/auth/login.py\"}"'
check "unlocked file is allowed"               '! guard bob Edit "{\"file_path\":\"src/web/app.js\"}"'
check "bash redirect into a locked file"       'guard bob Bash "{\"command\":\"echo x > src/auth/a.py\"}"'
check "bash sed -i on a locked file"           'guard bob Bash "{\"command\":\"sed -i s/a/b/ README.md\"}"'
check "bash read of a locked file is allowed"  '! guard bob Bash "{\"command\":\"grep x src/auth/a.py\"}"'
check "direct write to the board is denied"    'guard alice Bash "{\"command\":\"echo x >> $SLOG_BOARD\"}"'
check "stale locks are not enforced"           '! SLOG_STALE_HOURS=-1 guard bob Edit "{\"file_path\":\"src/auth/login.py\"}"'

echo "cr-notes + cr-context-hook"
(cd "$T/proj" && SLOG_TAG=alice "$HERE/bin/cr-notes" dont "don't bump pg" >/dev/null)
ctx=$(printf '{"hook_event_name":"SessionStart","source":"compact","cwd":"%s"}' "$T/proj" \
      | SLOG_TAG=bob python3 "$HERE/hooks/cr-context-hook.py" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')
check "SessionStart injects the locks"  'grep -q "api:auth — alice" <<<"$ctx"'
check "…and how to hand files/clips in Chat"  'grep -q "Download card" <<<"$ctx"'
check "…and the project notes"          'grep -q "bump pg" <<<"$ctx"'
printf '{"hook_event_name":"SessionEnd","reason":"exit"}' | SLOG_TAG=alice python3 "$HERE/hooks/cr-context-hook.py"
locks; check "SessionEnd frees the session's locks" '! grep -q alice "$T/locks"'
SLOG_TAG=carol "$HERE/bin/slog" take db >/dev/null; SLOG_TAG=carol "$HERE/bin/slog" free db >/dev/null
SLOG_TAG=carol "$HERE/bin/slog" "migrated the schema" >/dev/null
ctx=$(printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$T/proj" | SLOG_TAG=bob python3 "$HERE/hooks/cr-context-hook.py" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')
check "a closed TAKE/FREE pair folds into one line" 'grep -q "carol · 🔓 locks released: db" <<<"$ctx" && ! grep -q "TAKE \[db\]" <<<"$ctx"'
check "…and the feed line after it survives"       'grep -q "migrated the schema" <<<"$ctx"'
ctx=$(printf '{"hook_event_name":"SessionStart","cwd":"%s"}' "$T/proj" \
      | SLOG_BOARD="$T/empty/SESSIONS.md" SLOG_TAG=bob python3 "$HERE/hooks/cr-context-hook.py" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')
check "no locks is said explicitly"                'grep -q "Active locks: none." <<<"$ctx"'

echo "cr-exclusive"
SLOG_TAG=alice "$HERE/bin/cr-exclusive" res -- sleep 2 & sleep 0.3
SLOG_TAG=bob "$HERE/bin/cr-exclusive" -w 0 res -- true 2>/dev/null; rc=$?
check "busy resource fails fast with 75" '[ $rc = 75 ]'
SLOG_TAG=bob "$HERE/bin/cr-exclusive" -w 5 res -- true 2>/dev/null; rc=$?
check "…and is granted once free" '[ $rc = 0 ]'; wait
"$HERE/bin/cr-exclusive" res -- sh -c 'exit 3'; rc=$?
check "command exit code passes through" '[ $rc = 3 ]'

echo "blocked badge clears on a 'no' (hook + pane)"
if command -v tmux >/dev/null; then
  tmux -L crtest new-session -d -s claude1 -x 120 -y 30 "bash --norc"
  tmux -L crtest send-keys -t =claude1: 'clear; printf "Do you want to proceed?\n❯ 1. Yes\n  3. No, and tell Claude what to do differently\n"' Enter
  sleep 0.5
  res=$(CR_TMUX_SOCKET=crtest python3 - "$HERE" "$T" <<'EOF'
import importlib.util, json, os, sys, time
here, t = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("srv", os.path.join(here, "server", "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
os.makedirs(os.path.join(t, "state", "state"), exist_ok=True)
def put(st, ts): json.dump({"state": st, "ts": ts, "pane": ""}, open(os.path.join(t, "state", "state", "claude1.json"), "w"))
out = []
put("blocked", 1000); out.append(srv.effective_state("claude1"))
os.system("tmux -L crtest send-keys -t =claude1: clear Enter"); time.sleep(0.4)
out.append(srv.effective_state("claude1"))
put("blocked", 2000); out.append(srv.effective_state("claude1"))
print(" ".join(out))
EOF
)
  check "box on screen → blocked"                 '[ "$(cut -d" " -f1 <<<"$res")" = blocked ]'
  check "box gone after being seen → idle"        '[ "$(cut -d" " -f2 <<<"$res")" = idle ]'
  check "box never seen → trust the hook (blocked)" '[ "$(cut -d" " -f3 <<<"$res")" = blocked ]'
else
  echo "  (tmux not installed: skipped)"
fi

echo "blocked badge with parallel tool calls + a 'no' read from the transcript"
res=$(python3 - "$HERE" "$T" <<'EOF'
import importlib.util, io, json, os, sys, types
here, t = sys.argv[1], sys.argv[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
hook = load("hook", os.path.join(here, "hooks", "cr-state-hook.py"))
hook.STATE_DIR = os.path.join(t, "pstate"); hook.tmux_session = lambda: "claude1"
out, now = [], [1000]
hook.time = types.SimpleNamespace(time=lambda: now[0])
def fire(name, tool="", inp=None, **kw):
    now[0] += 1
    sys.stdin = io.StringIO(json.dumps(dict(hook_event_name=name, tool_name=tool, tool_input=inp, **kw)))
    hook.main()
    return json.load(open(os.path.join(t, "pstate", "claude1.json")))
# the measured sequence: a sleep and a Read run as one batch, the Read waits for permission
fire("UserPromptSubmit")
fire("PreToolUse", "Bash", {"command": "sleep 8"})
fire("PreToolUse", "Read", {"file_path": "/etc/hostname"})
s = fire("PermissionRequest", "Read", {"file_path": "/etc/hostname"}); out.append(s["state"]); ts = s["ts"]
s = fire("Notification", notification_type="permission_prompt"); out.append(str(s["ts"] == ts))
s = fire("PostToolUse", "Bash", {"command": "sleep 8"}); out.append(s["state"])
s = fire("PostToolUse", "Read", {"file_path": "/etc/hostname"}); out.append(s["state"])
# a "no": no hook fires, the transcript records it (after a 100 KB+ line, as measured)
srv = load("srv", os.path.join(here, "server", "server.py"))
tr = os.path.join(t, "reject.jsonl")
def line(ts, **kw): return json.dumps(dict(timestamp=ts, **kw), separators=(",", ":"))
with open(tr, "w") as fh:
    fh.write(line("2026-01-01T00:00:00.000Z", type="assistant") + "\n")
    fh.write(line("2026-01-01T00:00:30.000Z", type="user", toolUseResult="User rejected tool use") + "\n")
    fh.write(line("2026-01-01T00:00:30.002Z", type="attachment", pad="x" * 150000) + "\n")
t0 = 1767225600   # 2026-01-01T00:00:00Z
out.append(str(srv.rejected_since(tr, t0 + 10)))   # the dialog opened at :10 → answered "no"
out.append(str(srv.rejected_since(tr, t0 + 40)))   # a newer dialog (:40) → still waiting
print(" ".join(out))
EOF
)
check "PermissionRequest → blocked"                        '[ "$(cut -d" " -f1 <<<"$res")" = blocked ]'
check "late Notification keeps the same episode"          '[ "$(cut -d" " -f2 <<<"$res")" = True ]'
check "a sibling call finishing does not clear the badge" '[ "$(cut -d" " -f3 <<<"$res")" = blocked ]'
check "the approved call running clears it"               '[ "$(cut -d" " -f4 <<<"$res")" = working ]'
check "a 'no' in the transcript is found past a 150 KB line" '[ "$(cut -d" " -f5 <<<"$res")" = True ]'
check "…but an older 'no' does not clear a newer dialog"  '[ "$(cut -d" " -f6 <<<"$res")" = False ]'

echo "cr-hist + cr-history-hook"
HP="$T/hproj"; mkdir -p "$HP/src"
git -C "$HP" init -q; git -C "$HP" config user.email t@t; git -C "$HP" config user.name t
echo v1 > "$HP/src/a.txt"; git -C "$HP" add -A; git -C "$HP" commit -qm "project's own commit"
(cd "$HP" && "$HERE/bin/cr-hist" init >/dev/null)
hh() { printf '%s' "$2" | SLOG_TAG="$1" CLAUDE_CODE_SESSION_ID="$3" python3 "$HERE/hooks/cr-history-hook.py"; }
echo v2 > "$HP/src/a.txt"
hh alice '{"tool_name":"Edit","tool_input":{"file_path":"'"$HP"'/src/a.txt"},"cwd":"'"$HP"'"}' aaaaaaaa-0000-0000-0000-000000000000
echo new > "$HP/src/b.txt"
hh bob '{"tool_name":"Bash","tool_input":{"command":"x"},"cwd":"'"$HP"'"}' bbbbbbbb-0000-0000-0000-000000000000
(cd "$HP" && env -u CLAUDE_CODE_SESSION_ID SLOG_TAG=alice "$HERE/bin/cr-hist" changed) > "$T/h1"
check "changed shows the OTHER session's file"   'grep -q "bob/bash  src/b.txt" "$T/h1"'
check "…and hides your own"                      '! grep -q "alice  src/a.txt" "$T/h1"'
(cd "$HP" && SLOG_TAG=alice CLAUDE_CODE_SESSION_ID=aaaaaaaa-0000-0000-0000-000000000000 "$HERE/bin/cr-hist" changed) > "$T/h1s"
check "…your own = this conversation (Session:)" '! grep -q "alice  src/a.txt" "$T/h1s" && grep -q "bob/bash  src/b.txt" "$T/h1s"'
(cd "$HP" && SLOG_TAG=alice CLAUDE_CODE_SESSION_ID=aaaaaaaa-1111-1111-1111-111111111111 "$HERE/bin/cr-hist" changed) > "$T/h1c"
check "…on demand, the earlier conversation shows" 'grep -q "alice  src/a.txt.*earlier conversation" "$T/h1c"'
check "…and what it hides is counted"            'grep -q "(1 of this conversation.s own hidden)" "$T/h1s"'
for v in b2 b3; do echo $v > "$HP/src/b.txt"; hh bob '{"tool_name":"Bash","tool_input":{"command":"x"},"cwd":"'"$HP"'"}' bbbbbbbb-0000-0000-0000-000000000000; done
(cd "$HP" && env -u CLAUDE_CODE_SESSION_ID SLOG_TAG=alice "$HERE/bin/cr-hist" changed) > "$T/h1g"
check "repeated saves collapse into one line ×N" '[ "$(grep -c "bob/bash  src/b.txt" "$T/h1g")" = 1 ] && grep -q "bob/bash  src/b.txt  ×3" "$T/h1g"'
(cd "$HP" && "$HERE/bin/cr-hist" who src/a.txt) > "$T/h2"
check "who links the change to its session"      'grep -q "alice  src/a.txt  s:aaaaaaaa" "$T/h2"'
(cd "$HP" && "$HERE/bin/cr-hist" prev src/a.txt "$T/a.prev") >/dev/null
check "prev returns the version before"          '[ "$(cat "$T/a.prev")" = v1 ]'
check "the project's own git is untouched"       '[ "$(git -C "$HP" rev-list --count HEAD)" = 1 ]'
echo v3 > "$HP/src/a.txt"   # alice (window) edits again in the conversation before her /clear
hh alice '{"tool_name":"Edit","tool_input":{"file_path":"'"$HP"'/src/a.txt"},"cwd":"'"$HP"'"}' aaaaaaaa-0000-0000-0000-000000000000
printf '{"hook_event_name":"SessionStart","source":"clear","session_id":"aaaaaaaa-1111-1111-1111-111111111111","cwd":"%s"}' "$HP" \
  | SLOG_TAG=alice python3 "$HERE/hooks/cr-context-hook.py" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])' \
  > "$T/hctxall"; grep -E "src/(a|b)\.txt" "$T/hctxall" > "$T/hctx"
check "after /clear the context shows only OTHER sessions" 'grep -q "bob/bash  src/b.txt" "$T/hctx" && ! grep -q "alice  src/a.txt" "$T/hctx"'
check "…with one line pointing to its own" 'grep -q "own changes (2 in 12h" "$T/hctxall"'

echo "cr-cc (terminal dashboard)"
if command -v tmux >/dev/null; then
  tmux -L cctest new-session -d -s claude1 -x 120 -y 30 "bash --norc"
  tmux -L cctest new-session -d -s claude2 -x 120 -y 30 "bash --norc"
  tmux -L cctest send-keys -t =claude1: 'clear; printf "Do you want to proceed?\n❯ 1. Yes\n"' Enter
  sleep 0.5; mkdir -p "$CR_STATE_DIR/state"; now=$(date +%s)
  echo "{\"state\":\"blocked\",\"ts\":$now}" > "$CR_STATE_DIR/state/claude1.json"
  echo "{\"state\":\"blocked\",\"ts\":$((now-60))}" > "$CR_STATE_DIR/state/claude2.json"
  CR_TMUX_SOCKET=cctest CR_CONFIG=/nonexistent CR_SESSIONS="claude1 claude2" "$HERE/bin/cr-cc" > "$T/cc"
  check "prompt on screen → BLOCKED"            'grep -qE "^claude1 +BLOCKED" "$T/cc"'
  check "hook says blocked, box gone → idle"    'grep -qE "^claude2 +idle" "$T/cc"'
  CR_TMUX_SOCKET=cctest CR_CONFIG=/nonexistent CR_SESSIONS="claude1 claude2" "$HERE/bin/cr-cc" reply other x 2>/dev/null; rc=$?
  check "reply refuses sessions outside the allowlist" '[ $rc = 2 ]'
  tmux -L cctest kill-server 2>/dev/null
else
  echo "  (tmux not installed: skipped)"
fi

echo "chat view (server/chat.py)"
if command -v tmux >/dev/null; then
  mkdir -p "$T/bin" "$CR_STATE_DIR/state" "$CR_STATE_DIR/usage"
  ln -sf "$(command -v sleep)" "$T/bin/claude"          # a process named "claude" = Claude in the foreground
  tmux -L crchat new-session -d -s claude1 -x 100 -y 30 "bash --norc"
  tmux -L crchat new-session -d -s claude2 -x 100 -y 30 "bash --norc"
  tmux -L crchat send-keys -t =claude1: "clear; printf '────────────\n Bash command\n   rm -rf build\n   Clean the build dir\n Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don'\''t ask again for rm\n      commands in this project\n   3. No\n'; exec $T/bin/claude 60" Enter
  sleep 0.6
  TR="$T/transcript.jsonl"
  cat > "$TR" <<'JSONL'
{"type":"user","message":{"content":"clean the build<system-reminder>hidden</system-reminder>"}}
{"type":"assistant","message":{"content":[{"type":"text","text":"Sure — **cleaning** it."},{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"rm -rf build","description":"Clean the build dir"}}]}}
JSONL
  pane=$(tmux -L crchat display -p -t =claude1: '#{pane_id}')
  echo "{\"hook_event_name\":\"PreToolUse\",\"transcript_path\":\"$TR\"}" \
    | TMUX="/tmp/tmux-$(id -u)/crchat,0,0" TMUX_PANE="$pane" python3 "$HERE/hooks/cr-state-hook.py"
  check "state hook records the transcript path" 'grep -q "\"transcript\": \"$TR\"" "$CR_STATE_DIR/state/claude1.json"'
  echo '{"rate_limits":{"five_hour":{"used_percentage":7,"resets_at":1},"seven_day":{"used_percentage":31,"resets_at":2}},"context_window":{"used_percentage":12}}' \
    > "$CR_STATE_DIR/usage/claude1.json"
  CR_TMUX_SOCKET=crchat python3 - "$HERE" "$T" > "$T/chat" <<'EOF'
import importlib.util, json, os, sys
here, t = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(here, "server")); import chat
chat.init(["tmux", "-L", "crchat"], os.environ["CR_STATE_DIR"])
ok = ["claude1", "claude2"]
d = chat.get("claude1", "", 0, ok)
print("kinds", ",".join(e["k"] for e in d["events"]))
print("reminder", any("hidden" in e.get("text", "") for e in d["events"]))
sc = d["screen"] or {}
print("question", sc.get("question"))
print("opt2", [o["label"] for o in sc.get("opts", []) if o["n"] == "2"])
print("detail", "rm -rf build" in sc.get("detail", ""))
d2 = chat.get("claude1", d["sid"], d["off"], ok)
print("incremental", len(d2["events"]))
open(os.path.join(t, "transcript.jsonl"), "a").write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}) + "\n")
d3 = chat.get("claude1", d["sid"], d["off"], ok)
print("appended", ",".join(e["k"] for e in d3["events"]))
print("away", chat.get("claude2", "", 0, ok).get("away"))
print("notallowed", "error" in chat.get("claude9", "", 0, ok))
print("badkey", chat.send("claude1", ok, key="C-x"), chat.send("claude9", ok, text="x"))
u = chat.usage(); print("usage", u["five_hour"]["pct"], u["seven_day"]["pct"], u["ctx"].get("claude1"))
json.dump({"transcript": os.path.join(t, "not-yet.jsonl")}, open(os.path.join(os.environ["CR_STATE_DIR"], "state", "claude1.json"), "w"))
os.remove(os.path.join(os.environ["CR_STATE_DIR"], "usage", "claude1.json"))
d4 = chat.get("claude1", "", 0, ok); print("fresh", d4.get("fresh"), len(d4["events"]))
open(os.path.join(t, "report.zip"), "w").write("x")
chat.init(["tmux", "-L", "crchat"], os.environ["CR_STATE_DIR"], [os.path.realpath(t)])
fs = chat.files_in(f"here: {t}/report.zip and /etc/hostname and {t}/missing.csv")
print("files", ",".join(f["n"] for f in fs))
EOF
  check "transcript → user, text, tool call"        'grep -q "^kinds user,asst,tool$" "$T/chat"'
  check "system reminders never reach the chat"     'grep -q "^reminder False$" "$T/chat"'
  check "permission question is read"               'grep -q "^question Do you want to proceed?$" "$T/chat"'
  check "a wrapped option is joined back"           'grep -q "Yes, and don.t ask again for rm commands in this project" "$T/chat"'
  check "…and the box says WHAT is asked"          'grep -q "^detail True$" "$T/chat"'
  check "reads are incremental (nothing new → 0)"   'grep -q "^incremental 0$" "$T/chat"'
  check "…and only the new line comes next"         'grep -q "^appended res$" "$T/chat"'
  check "a tile at the shell is 'away', not a chat" 'grep -q "^away True$" "$T/chat"'
  check "sessions outside CR_SESSIONS are refused"  'grep -q "^notallowed True$" "$T/chat"'
  check "keys outside the whitelist are refused"    'grep -q "^badkey False False$" "$T/chat"'
  check "plan usage + context % are read"           'grep -q "^usage 7 31 12$" "$T/chat"'
  check "a new conversation is never shown as another one" 'grep -q "^fresh True 0$" "$T/chat"'
  check "a file Claude mentions → Download card (only if it exists, inside the roots)" 'grep -q "^files report.zip$" "$T/chat"'
  tmux -L crchat kill-server 2>/dev/null
else
  echo "  (tmux not installed: skipped)"
fi

echo "cr-clip keeps the previous clips"
CD="$T/clipstate"; mkdir -p "$CD"
for x in one two two three; do CR_STATE_DIR="$CD" "$HERE/bin/cr-clip" "$x" >/dev/null; done
check "current clip is the last one"            '[ "$(cat "$CD/clip.txt")" = three ]'
check "the previous one moved to clip-1"        '[ "$(cat "$CD/clip-1.txt")" = two ]'
check "setting the same text doesn't rotate"    '[ "$(cat "$CD/clip-2.txt")" = one ] && [ ! -e "$CD/clip-3.txt" ]'

echo "chat: clip cards only for clips that were really loaded"
if command -v node >/dev/null; then
  python3 - "$HERE/web/chat.js" > "$T/clip.js" <<'EOF'
import sys
s = open(sys.argv[1]).read(); i = s.index("function clipFromCmd(cmd){"); j = s.index("function toolBody(e){")
print(s[i:j])
print(r"""
const cases = [
  ["C=/x/bin/cr-clip; python3 - \"$C\" <<'EOF'\nprint(1)\nEOF", null],
  ["cat >> README.md <<'EOF'\nuse `cr-clip \"x\"` or `<<'EOF' | cr-clip`\nEOF", null],
  ["cat <<'EOF' | cr-clip\na\nb\nEOF", "a\nb"],
  ["cr-clip <<'EOF'\nsolo\nEOF", "solo"],
  ['cr-clip "npm run deploy"', "npm run deploy"],
  ["cr-clip --show", null]];
let bad = 0; for(const [c, want] of cases){ if(clipFromCmd(c) !== want){ bad++; console.error("clip case failed:", JSON.stringify(c)); } }
process.exit(bad);""")
EOF
  check "a clip mentioned in text or a path in a variable is not a clip" 'node "$T/clip.js"'
else
  echo "  (node not installed: skipped)"
fi

echo "chat: first-run screens and the unsent input box (parsed from the screen)"
python3 - "$HERE/server" > "$T/setup.out" <<'EOF'
import sys; sys.path.insert(0, sys.argv[1]); import chat
trust = " Quick safety check: Is this a project you created or one you trust?\n\n Security guide\n\n ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel\n"
s = chat.setup_screen("x", trust); print("trust", [o["label"] for o in s["opts"]], s["cur"])
theme = " Choose the text style\n\n   1. Auto (match terminal)\n ❯ 2. Dark mode ✔\n   3. Light mode\n\n ╌╌╌╌\n  1  function greet() {\n"
s = chat.setup_screen("x", theme); print("theme", len(s["opts"]), s["cur"], s["opts"][1]["label"])
url = " ███▓\n Browser didn't open? Use the url below to sign in\nhttps://example.com/auth?a=1&b\n=2&c=3\n Paste code here if prompted >\n"
s = chat.setup_screen("x", url); print("url", s["url"], s["code"])
print("moving", chat.setup_screen("x", " some text\n still drawing\n"))
rule = "─" * 40
print("draft", repr(chat.draft("x", f"❯ sent before\n{rule}\n❯ line one\n  line two\n{rule}\n  ⏸ manual mode on\n")))
print("empty", repr(chat.draft("x", f"{rule}\n❯ \n{rule}\n")))
print("hint", repr(chat.draft("x", f"{rule}\n\x1b[39m❯ \x1b[2mTry something\x1b[22m\n{rule}\n")))
print("nobox", chat.draft("x", " ❯ 1. Yes\n   2. No\n"))
EOF
check "trust this folder: options without numbers, cursor on the first" 'grep -qF "trust ['"'"'No, exit'"'"', '"'"'Yes, I trust this folder'"'"'] 0" "$T/setup.out"'
check "theme list: cursor on option 2, the preview below is not an option" 'grep -qF "theme 3 1 Dark mode" "$T/setup.out"'
check "the wrapped sign-in link is joined back"      'grep -qF "url https://example.com/auth?a=1&b=2&c=3 True" "$T/setup.out"'
check "a screen with nothing to answer is not a card" 'grep -qF "moving None" "$T/setup.out"'
check "text left in the input box is read, multi-line" "grep -qF \"draft 'line one\\\\nline two'\" \"\$T/setup.out\""
check "empty box and dimmed hint are not a draft"    "grep -qF \"empty ''\" \"\$T/setup.out\" && grep -qF \"hint ''\" \"\$T/setup.out\""
check "no input box on screen → None"                'grep -qF "nobox None" "$T/setup.out"'

python3 - "$HERE/server" "$T" > "$T/keep.out" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1]); import chat
up = os.path.join(sys.argv[2], "uploads", "2026-01-01"); os.makedirs(up, exist_ok=True)
img = os.path.join(up, "120000_photo.jpg"); open(img, "w").close()
sent = []
chat.draft = lambda s, *a: " " + img + " "            # the upload dialog typed this, no Enter
chat.clear_draft = lambda s: True
chat._run = lambda cmd, inp=None, **k: (sent.append(inp) if inp else None) or type("R", (), {"returncode": 0})()
chat.send("s", ["s"], text="look at this")
print("kept" if sent and img in sent[0] and sent[0].startswith("look at this") else "lost " + repr(sent))
EOF
check "an uploaded path left in the input box joins the chat message" 'grep -qx kept "$T/keep.out"'

[ $FAIL = 0 ] && echo "all tests passed" || { echo "SOME TESTS FAILED"; exit 1; }
