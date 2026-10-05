#!/usr/bin/env bash
# install.sh — one-step setup. Safe to run again: every step is idempotent and backs up what it edits.
#
#   ./install.sh                         check dependencies + create controlroom.env
#   ./install.sh --workdir ~/myproject   …and point new sessions at your project
#   ./install.sh --all                   …plus the three optional steps below
#     --hooks    add the hooks (state, lock guard, session context, history) and the silent
#                statusLine (plan usage for the UI) to ~/.claude/settings.json
#                (backup: settings.json.bak-<date>)
#     --skills   install the skill-sync skill into ~/.claude/skills/
#     --tmux     append the recommended settings to ~/.tmux.conf (once, between markers)
#
# It never touches anything else, never downloads anything, and never needs sudo.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DO_HOOKS=0; DO_SKILLS=0; DO_TMUX=0; WORKDIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --hooks) DO_HOOKS=1;; --skills) DO_SKILLS=1;; --tmux) DO_TMUX=1;;
    --all) DO_HOOKS=1; DO_SKILLS=1; DO_TMUX=1;;
    --workdir) WORKDIR="$(cd "${2:?--workdir needs a path}" && pwd)"; shift;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 1;;
  esac; shift
done
ok(){ printf '  \033[32m✔\033[0m %s\n' "$*"; }
no(){ printf '  \033[31m✗\033[0m %s\n' "$*"; }
info(){ printf '  · %s\n' "$*"; }

echo "1. Dependencies"
MISSING=0
if command -v tmux >/dev/null; then
  v=$(tmux -V | grep -oE '[0-9]+\.[0-9]+' | head -1)
  if awk -v v="$v" 'BEGIN{exit !(v>=3.3)}'; then ok "tmux $v"; else no "tmux $v — need ≥ 3.3 (for clipboard passthrough)"; MISSING=1; fi
else no "tmux — install it (sudo apt install tmux)"; MISSING=1; fi
if command -v ttyd >/dev/null; then
  tv=$(ttyd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
  if awk -v v="$tv" 'BEGIN{split(v,a,"."); exit !(a[1]>1 || (a[1]==1 && a[2]>=7))}'; then ok "ttyd $tv"
  else no "ttyd $tv — need ≥ 1.7 (older versions lack -W). Get the official binary: see README → Quick start"; MISSING=1; fi
else no "ttyd — install it (apt install ttyd, or a release binary from its project page)"; MISSING=1; fi
if command -v python3 >/dev/null; then ok "python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
else no "python3"; MISSING=1; fi
command -v claude >/dev/null && ok "claude (Claude Code)" || info "claude not on PATH — tiles will fall back to a shell until it is"
command -v magick >/dev/null || command -v convert >/dev/null && ok "ImageMagick (image optimization)" || info "optional: ImageMagick, to shrink uploaded images for tokens"
command -v pdftotext >/dev/null && ok "pdftotext (PDF → text)" || info "optional: poppler-utils, to extract text from uploaded PDFs"
command -v dot >/dev/null && ok "Graphviz (diagrams in the Chat view)" || info "recommended: graphviz, to draw \`\`\`dot blocks as diagrams in the Chat view"
[ -f "${CR_KATEX_DIR:-/usr/share/javascript/katex}/katex.min.js" ] && ok "KaTeX (formulas in the Chat view)" || info "recommended: libjs-katex (or CR_KATEX_DIR), to render \$…\$ formulas in the Chat view"
command -v flock >/dev/null && ok "flock (slog locking)" || { no "flock (util-linux) — needed by slog"; MISSING=1; }

echo "2. Configuration"
if [ -f "$HERE/controlroom.env" ]; then ok "controlroom.env already exists (left untouched)"
else
  cp "$HERE/controlroom.env.example" "$HERE/controlroom.env"
  ok "created controlroom.env from the example"
fi
if [ -n "$WORKDIR" ]; then
  python3 - "$HERE/controlroom.env" "$WORKDIR" <<'EOF'
import re, sys
p, wd = sys.argv[1], sys.argv[2]
s = open(p).read()
s = re.sub(r'^#?\s*CR_WORKDIR=.*$', 'CR_WORKDIR="%s"' % wd, s, count=1, flags=re.M)
open(p, "w").write(s)
EOF
  ok "CR_WORKDIR=$WORKDIR"
  if [ -d "$WORKDIR/.git" ] && ! grep -qx '.controlroom/' "$WORKDIR/.gitignore" 2>/dev/null; then
    info "tip: add '.controlroom/' to $WORKDIR/.gitignore (uploads and the outbox live there)"
  fi
fi
chmod +x "$HERE"/bin/* "$HERE/start.sh"

if [ "$DO_HOOKS" = 1 ]; then
  echo "3. Claude Code hooks (state, guard, context, history)"
  python3 - "$HERE/hooks" <<'EOF'
import json, os, shutil, sys, time
hooks_dir = sys.argv[1]
path = os.path.expanduser("~/.claude/settings.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
data = {}
if os.path.exists(path):
    with open(path) as fh:
        data = json.load(fh)                      # invalid JSON → abort loudly, change nothing
    shutil.copy2(path, path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
# (script, event, matcher)
WANT = [("cr-state-hook.py", ev, "*" if "Tool" in ev or ev == "PermissionRequest" else None)
        for ev in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                   "PermissionRequest", "Notification", "Stop", "SessionEnd")]
WANT += [("cr-guard-hook.py", "PreToolUse", "Edit|Write|MultiEdit|NotebookEdit|Bash"),
         ("cr-context-hook.py", "SessionStart", None),
         ("cr-context-hook.py", "SessionEnd", None),
         ("cr-history-hook.py", "PostToolUse", "Edit|Write|MultiEdit|NotebookEdit|Bash")]
hooks = data.setdefault("hooks", {})
added = 0
for script, ev, matcher in WANT:
    groups = hooks.setdefault(ev, [])
    if any(script in h.get("command", "") for g in groups for h in g.get("hooks", [])):
        continue
    g = {"hooks": [{"type": "command", "command": "python3 " + os.path.join(hooks_dir, script)}]}
    if matcher:
        g["matcher"] = matcher
    groups.append(g)
    added += 1
# Silent statusLine: plan usage (session / week) + context % for the UI. Never replace one you already have.
sl = data.get("statusLine")
if not sl:
    data["statusLine"] = {"type": "command", "command": "python3 " + os.path.join(hooks_dir, "cr-statusline.py")}
    added += 1
elif "cr-statusline.py" not in (sl.get("command") or ""):
    print("  \033[33m!\033[0m you already have a statusLine: plan usage in the UI needs cr-statusline.py fed the same stdin (see its header)")
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print("  \033[32m✔\033[0m %s: %s" % (path, ("added %d hook entries" % added) if added else "already installed"))
EOF
  info "restart running Claude sessions to pick up the hooks"
  info "history is opt-in per project: run  cr-hist init  inside a project to start recording it"
fi

if [ "$DO_SKILLS" = 1 ]; then
  echo "4. Skills"
  DEST="$HOME/.claude/skills/skill-sync"
  if [ -e "$DEST" ]; then ok "skill-sync already installed at $DEST (left untouched)"
  else mkdir -p "$HOME/.claude/skills"; cp -r "$HERE/skills/skill-sync" "$DEST"; ok "installed skill-sync → $DEST"; fi
  info "fill in $DEST/references/owners-map.md for your project; create skills with: skill-new <name>"
fi

if [ "$DO_TMUX" = 1 ]; then
  echo "5. tmux"
  CONF="$HOME/.tmux.conf"
  if grep -q '>>> claude-code-control-room >>>' "$CONF" 2>/dev/null; then ok "~/.tmux.conf already has the block"
  else
    [ -f "$CONF" ] && cp "$CONF" "$CONF.bak-$(date +%Y%m%d-%H%M%S)"
    { echo; echo "# >>> claude-code-control-room >>>"; cat "$HERE/examples/tmux.conf"; echo "# <<< claude-code-control-room <<<"; } >> "$CONF"
    ok "appended recommended settings to ~/.tmux.conf"
    info "reload a running tmux with: tmux source-file ~/.tmux.conf"
  fi
fi

echo
if [ "$MISSING" = 1 ]; then echo "Install the missing dependencies above, then run ./start.sh"; exit 1; fi
cat <<EOF
Ready. Next:
  1. Add the tools to your PATH (e.g. in ~/.bashrc):
       export PATH="\$PATH:$HERE/bin"
  2. Paste examples/CLAUDE.md-snippet.md into your project's CLAUDE.md
  3. ./start.sh   →  http://127.0.0.1:7680
  4. Before using it from another device, read README → Security. It has NO authentication.
EOF
[ "$DO_HOOKS$DO_SKILLS$DO_TMUX" = "000" ] && echo "  (optional: ./install.sh --all  adds the hooks, the skill-sync skill and the tmux settings)"
exit 0
