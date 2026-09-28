#!/usr/bin/env bash
# setup.sh — runs ONCE when the codespace is created (postCreateCommand).
# Installs the dependencies, Claude Code, and configures the control room to work on this repo.
set -euo pipefail
WS="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ dependencies (python3, tmux, ttyd 1.7 from Ubuntu 24.04, poppler for PDF text; the base image has no python3)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 tmux ttyd poppler-utils >/dev/null

echo "→ Claude Code (official native installer → ~/.local/bin/claude)"
command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash

echo "→ control room config"
cat > "$WS/controlroom.env" <<CONF
CR_BIND=127.0.0.1
CR_PORT=7680
CR_SESSIONS="claude1 claude2 claude3 shell"
CR_SHELL_SESSIONS="shell"
CR_WORKDIR="$WS"
CR_CLAUDE_CMD="claude"
CONF
bash "$WS/install.sh" --all --workdir "$WS" || true   # exits 1 only if a dependency is missing

# tmux starts login shells (bash -l), which read ~/.profile, not ~/.bashrc
line="export PATH=\"\$HOME/.local/bin:\$PATH:$WS/bin\""
for f in ~/.profile ~/.bashrc; do grep -qxF "$line" "$f" 2>/dev/null || echo "$line" >> "$f"; done
echo "✔ ready — the panel opens on port 7680 (Ports tab → Control Room)"
