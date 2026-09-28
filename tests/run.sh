#!/usr/bin/env bash
# tests/run.sh — behavior tests for the coordination pieces. Needs bash, python3, tmux, flock.
# Runs in CI (smoke job) and locally:  bash tests/run.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'tmux -L crtest kill-server 2>/dev/null; rm -rf "$T"' EXIT
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
check "…and the project notes"          'grep -q "bump pg" <<<"$ctx"'
printf '{"hook_event_name":"SessionEnd","reason":"exit"}' | SLOG_TAG=alice python3 "$HERE/hooks/cr-context-hook.py"
locks; check "SessionEnd frees the session's locks" '! grep -q alice "$T/locks"'

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
check "after /clear the earlier conversation shows" 'grep -q "alice  src/a.txt.*earlier conversation" "$T/h1c"'
check "…and what it hides is counted"            'grep -q "(1 of this conversation.s own hidden)" "$T/h1s"'
for v in b2 b3; do echo $v > "$HP/src/b.txt"; hh bob '{"tool_name":"Bash","tool_input":{"command":"x"},"cwd":"'"$HP"'"}' bbbbbbbb-0000-0000-0000-000000000000; done
(cd "$HP" && env -u CLAUDE_CODE_SESSION_ID SLOG_TAG=alice "$HERE/bin/cr-hist" changed) > "$T/h1g"
check "repeated saves collapse into one line ×N" '[ "$(grep -c "bob/bash  src/b.txt" "$T/h1g")" = 1 ] && grep -q "bob/bash  src/b.txt  ×3" "$T/h1g"'
(cd "$HP" && "$HERE/bin/cr-hist" who src/a.txt) > "$T/h2"
check "who links the change to its session"      'grep -q "alice  src/a.txt  s:aaaaaaaa" "$T/h2"'
(cd "$HP" && "$HERE/bin/cr-hist" prev src/a.txt "$T/a.prev") >/dev/null
check "prev returns the version before"          '[ "$(cat "$T/a.prev")" = v1 ]'
check "the project's own git is untouched"       '[ "$(git -C "$HP" rev-list --count HEAD)" = 1 ]'

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

[ $FAIL = 0 ] && echo "all tests passed" || { echo "SOME TESTS FAILED"; exit 1; }
